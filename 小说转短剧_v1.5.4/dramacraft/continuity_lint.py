# -*- coding: utf-8 -*-
"""创作纵深哨兵：伏笔账本（F 族）与命名节奏（T 族）——纯标准库确定性门。

设计定案（v1.4.0 设计方案 §二①）：
- 伏笔账本 = **append-only 事件流** `state/foreshadow_log.jsonl`，当前状态一律由 `replay` 重放派生，
  永不读-改-写「当前状态文件」——并发 3 双 worker 同时登记零丢账（复用 Ledger 单行+锁）。
- 重放即回滚：`pay` 事件仅当其认领集数 ∈ 当前有效缓存集才被采信；`--stale-cache redo` 删掉某集缓存后，
  该集认领自动作废，涉及伏笔自然回 `open`，无需任何快照镜像。
- F 族与 T 族同体同 CLI；结果以独立顶层键输出，不碰 validate.mjs 恒 12 的 gates 数组。
"""
import argparse
import hashlib
import json
import os
import re
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from dramacraft.ledger import Ledger, load_events

# 事件类型枚举
EV_PLANT = "plant"
EV_PAY = "pay"
EV_ABANDON = "abandon"


def foreshadow_log_path(workdir: Any) -> str:
    """伏笔账本事件流路径：<workdir>/state/foreshadow_log.jsonl"""
    return os.path.join(os.path.abspath(str(workdir)), "state", "foreshadow_log.jsonl")


def ep_content_hash(ep_data: Any) -> str:
    """单集内容指纹（去掉内部 _meta，规范化键序）。认领事件据此绑定「当时那一版剧本」。"""
    d = dict(ep_data) if isinstance(ep_data, dict) else {}
    d.pop("_meta", None)
    blob = json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.md5(blob).hexdigest()[:12]


def load_ep_hashes(workdir: Any) -> Dict[int, str]:
    """扫当前工作区已落盘的单集缓存，返回 {集号: 内容指纹}（重放 pay 的有效性底账）。"""
    return {ep.get("ep"): ep_content_hash(ep) for ep in _load_cached_episodes(workdir)}


# 同一路径复用同一 Ledger 实例 → 天然共用其线程锁，并发登记串行化（零新锁实现）
_ledger_cache: Dict[str, Ledger] = {}
_cache_lock = threading.Lock()


def _ledger_for(path: str) -> Ledger:
    with _cache_lock:
        lg = _ledger_cache.get(path)
        if lg is None:
            lg = Ledger(path)
            _ledger_cache[path] = lg
        return lg


def emit_event(workdir: Any, ev: str, ep: Optional[int] = None, **fields: Any) -> None:
    """向伏笔账本追加一条事件（单行 JSON，线程安全，写失败只警告不中断主流程）。"""
    _ledger_for(foreshadow_log_path(workdir)).emit(ev, ep=ep, **fields)


def replay(workdir: Any, valid_eps: Optional[Set[int]] = None,
           ep_hashes: Optional[Dict[int, str]] = None) -> Dict[str, Dict[str, Any]]:
    """重放事件流派生当前伏笔状态。

    :param valid_eps: 当前**有效缓存集**（过了断点安检的集号）。`pay` 事件仅当 `ep` 落在其中才被采信；
        缺省/None 视为空集——未确认的认领一律不采信（保守方向：宁多报不漏账）。
        `plant`/`abandon` 是登记与决策记录，与某集缓存存不存在无关，一律采信。
    :param ep_hashes: 可选的内容指纹底账 `{集号: 内容指纹}`。**给了就按指纹对账**：认领只在其绑定的
        那一版剧本仍是当前落盘版本时成立——`--stale-cache redo` 后重新生成的同一集内容变了，
        旧认领自动作废（否则会出现「重生成后旧认领复活、没兑现也被判已回收」的假放行）。
        缺省 None = 不做指纹校验（纯函数测试与旧数据场景）。
    :return: {伏笔ID: {status: open|paid|abandoned, text, plantedInEp, payoffPlannedEp, ...}}
    """
    valid: Set[int] = set(valid_eps) if valid_eps else set()
    check_hash = ep_hashes is not None
    states: Dict[str, Dict[str, Any]] = {}

    for e in load_events(foreshadow_log_path(workdir)):
        fid = str(e.get("id", "")).strip()
        if not fid:
            continue
        ev = e.get("event")
        st = states.setdefault(fid, {"id": fid, "status": "open"})

        if ev == EV_PLANT:
            if e.get("ep") is not None:
                st["plantedInEp"] = e.get("ep")
            if e.get("text") is not None:
                st["text"] = e.get("text")
            if e.get("payoffPlannedEp") is not None:
                st["payoffPlannedEp"] = e.get("payoffPlannedEp")
            st.setdefault("registeredBy", e.get("registeredBy") or "outline")
        elif ev == EV_PAY:
            if e.get("ep") not in valid:
                continue  # 重放即回滚：认领集缓存已失效，本次认领作废
            if check_hash and e.get("epHash") != ep_hashes.get(e.get("ep")):
                continue  # 该集已重新生成（内容指纹不符）→ 旧稿的认领随之作废
            st["status"] = "paid"
            st["paidInEp"] = e.get("ep")
            st["evidence"] = e.get("evidence") or ""
        elif ev == EV_ABANDON:
            st["status"] = "abandoned"
            st["abandonReason"] = e.get("abandonReason")
            st["decidedBy"] = e.get("decidedBy")

    return states


def _collect_outline_foreshadows(outline_data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """从大纲抽取伏笔登记项（缺 ID 自动补 F###）。返回 (合法登记项, 违规清单)。"""
    entries: List[Dict[str, Any]] = []
    bad: List[str] = []

    eps = outline_data.get("episodes", []) if isinstance(outline_data, dict) else []
    if not isinstance(eps, list):
        return entries, bad
    ep_nums = {ep.get("ep") for ep in eps if isinstance(ep, dict)}
    ep_nums = {n for n in ep_nums if isinstance(n, int) and not isinstance(n, bool)}

    seen_ids: Dict[str, str] = {}
    counter = 0

    for ep in eps:
        if not isinstance(ep, dict):
            continue
        n = ep.get("ep")
        raw = ep.get("foreshadows")
        if raw is None:
            continue  # 契约字段可选：旧大纲零改动照跑
        if not isinstance(raw, list):
            bad.append(f"[F1] 第 {n} 集 foreshadows 必须是列表（登记表格式不对，无法对账）")
            continue

        for i, f in enumerate(raw):
            where = f"第 {n} 集第 {i + 1} 条伏笔"
            if not isinstance(f, dict):
                bad.append(f"[F1] {where} 不是登记表（应为含 text/payoffInEp 的对象）")
                continue

            counter += 1
            fid = str(f.get("id", "")).strip() or f"F{counter:03d}"
            text = str(f.get("text", "")).strip()
            payoff = f.get("payoffInEp")
            ok = True

            if not text:
                bad.append(f"[F1] {where}（{fid}）缺 text：伏笔必须写成可追踪的事件描述，否则无人能对账")
                ok = False

            if fid in seen_ids:
                bad.append(f"[F1] 伏笔 ID 重复：{fid}（{where} 与 {seen_ids[fid]} 撞号，回收时认错门）")
                ok = False
            else:
                seen_ids[fid] = where

            if not isinstance(payoff, int) or isinstance(payoff, bool) or payoff < 1:
                bad.append(f"[F1] {where}（{fid}）payoffInEp 非法：{payoff!r}（必须是正整数集号）")
                ok = False
            elif isinstance(n, int) and payoff < n:
                bad.append(f"[F1] {where}（{fid}）计划回收集 {payoff} 早于埋设集 {n}：还没埋就要求回收")
                ok = False
            elif payoff not in ep_nums:
                bad.append(f"[F1] {where}（{fid}）计划回收集 {payoff} 不在大纲集号中"
                           f"（现有集号 {sorted(ep_nums)}）——孤儿伏笔永远不会被回收")
                ok = False

            if ok:
                entries.append({
                    "id": fid,
                    "text": text,
                    "plantedInEp": n,
                    "payoffPlannedEp": payoff,
                    "registeredBy": str(f.get("registeredBy", "")).strip() or "outline",
                })

    return entries, bad


def lint_registration(outline_data: Dict[str, Any]) -> List[str]:
    """F1 大纲闸：伏笔登记合法性（纯函数，与 O1-O6 同点同时机拒跑）。空列表 = 通过。"""
    return _collect_outline_foreshadows(outline_data)[1]


def _next_free_id(used: Set[str]) -> str:
    n = 1
    while f"F{n:03d}" in used:
        n += 1
    return f"F{n:03d}"


def register_from_outline(workdir: Any, outline_data: Dict[str, Any]) -> int:
    """把大纲里合法的伏笔登记项幂等入册。返回本次新登记条数。

    幂等按**文本**判定，编号只作次选：这样纯重排不会重复入册，而在队首插入新伏笔导致自动编号
    漂移时，新条目会自动改用下一个空闲编号入册——绝不因「编号被别的内容占着」而静默丢弃线索。
    """
    entries, _ = _collect_outline_foreshadows(outline_data)
    plants = [e for e in load_events(foreshadow_log_path(workdir))
              if e.get("event") == EV_PLANT]
    planted_texts = {str(e.get("text", "")).strip() for e in plants}
    used_ids = {str(e.get("id", "")).strip() for e in plants}

    n_new = 0
    for it in entries:
        if it["text"] in planted_texts:
            continue                    # 同一条伏笔已在册（重排 / 换号都在此兜住）
        fid = it["id"]
        if fid in used_ids:             # 编号被别的内容占着 → 另起新号，绝不静默丢弃
            fid = _next_free_id(used_ids)
        emit_event(workdir, EV_PLANT, ep=it["plantedInEp"], id=fid, text=it["text"],
                   payoffPlannedEp=it["payoffPlannedEp"], registeredBy=it["registeredBy"])
        used_ids.add(fid)
        planted_texts.add(it["text"])
        n_new += 1
    return n_new


# ============================================================================
# T 门族 —— 命名与节奏哨兵（与 F 族同模块同 CLI，纯函数零 IO）
# 血统：术语锁定三表 / 泛化命名禁令 / 同名冲突 = fbs workflow-s0.md S0-Init 模板原文；
#       T3 结构雷同 / T4 句长节奏 = fbs quality-check.md §5.1 B2-C / B2-A·S6 指标定义。
# ============================================================================

_GENERIC_NAME_RE = re.compile(r"^(?:小|大|老)[\u4e00-\u9fa5]{1,2}$")
_EP_PREFIX_RE = re.compile(r"^第\s*\d+\s*集[\s_\-—:：]*")
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?…]+")
_DASH = "——"


def _as_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _as_str_list(v: Any) -> List[str]:
    return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []


def _iter_beats(ep_data: Any):
    """遍历一集里所有拍（场号, 拍序号, beat 对象）；畸形输入安全跳过。"""
    for sc_idx, sc in enumerate(_as_dict(ep_data).get("scenes") or []):
        if not isinstance(sc, dict):
            continue
        sc_id = str(sc.get("sceneId") or sc.get("scene") or f"S{sc_idx + 1:02d}").strip()
        for b_idx, beat in enumerate(sc.get("flow") or []):
            if isinstance(beat, dict):
                yield sc_id, b_idx + 1, beat


def _ep_label(ep_n: Any) -> str:
    return f"第 {ep_n} 集" if isinstance(ep_n, int) and not isinstance(ep_n, bool) else "本集"


def build_naming_seeds(outline_data: Any, art_data: Any = None) -> Optional[Dict[str, Any]]:
    """汇总命名/术语种子。

    种子来源（术语锁定三表：标准写法 / 禁用变体 / 替换记录）：
    - 标准写法 = 大纲注册人物名与别名 + 美术场景/道具名 + 显式 `terms[].standard`；
    - 禁用变体 = 人物 `forbiddenNames` + 显式 `terms[].forbidden`；
    - 替换记录 = 本模块输出与 CLI 报告的命中清单（照搬 fbs「术语锁定记录」用法）。
    任何种子都没有时返回 None（调用方据此输出「跳过」，不误报不静默）。
    """
    outline = _as_dict(outline_data)
    art = _as_dict(art_data)

    names: List[str] = []
    aliases: Dict[str, str] = {}
    forbidden: List[str] = []
    terms: List[Dict[str, Any]] = []
    char_owner: Dict[str, List[str]] = {}
    id_to_name: Dict[str, str] = {}

    def _claim(name: str) -> None:
        if name and name not in names:
            names.append(name)

    def _claim_character(name: str, cid: str) -> None:
        if not name or not cid:
            return
        holders = char_owner.setdefault(name, [])
        if cid not in holders:
            holders.append(cid)

    for c in outline.get("characters") or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id", "")).strip() or str(c.get("name", "")).strip()
        nm = str(c.get("name", "")).strip()
        _claim(nm)
        _claim_character(nm, cid)
        if cid and nm:
            id_to_name[cid] = nm
        for a in _as_str_list(c.get("aliases")):
            aliases.setdefault(a, nm)
            _claim(a)
            _claim_character(a, cid)
        for f in _as_str_list(c.get("forbiddenNames")):
            if f not in forbidden:
                forbidden.append(f)

    for asset in (art.get("scenes") or []) + (art.get("props") or []):
        if isinstance(asset, dict):
            _claim(str(asset.get("name", "")).strip())

    for src in (outline.get("terms"), art.get("terms")):
        for t in src if isinstance(src, list) else []:
            if not isinstance(t, dict):
                continue
            std = str(t.get("standard", "")).strip()
            if not std:
                continue
            _claim(std)
            ban = [b for b in _as_str_list(t.get("forbidden")) if b]
            terms.append({"standard": std, "forbidden": ban,
                          "history": _as_str_list(t.get("history"))})
            for b in ban:
                if b not in forbidden:
                    forbidden.append(b)

    # 同名不同人只针对人物表内部；美术件与术语件同名是同一个东西，不算冲突
    clashing = {n: holders for n, holders in char_owner.items() if len(holders) > 1}
    if not names and not terms and not forbidden:
        return None
    return {
        "names": names,
        "aliases": aliases,
        "clashing": clashing,
        "forbiddenNames": forbidden,
        "terms": terms,
        "name_set": set(names),
        "id_to_name": id_to_name,
    }


def ep_level_checks(ep_data: Any, seeds: Optional[Dict[str, Any]]) -> List[str]:
    """T1 术语锁定 + T2 命名冲突的单集检查（确定性，无 IO）。无种子返回空列表。"""
    if not seeds:
        return []
    bad: List[str] = []
    label = _ep_label(_as_dict(ep_data).get("ep"))

    # T1/T2 文本层命中：台词与动作行全文扫描（禁用变体 / 禁用称呼 / 同名不同人）
    # 同一禁用词只出一条——T1 带标准写法，优先于 T2 的泛化提示
    text_rules: List[Any] = []
    seen_needles: Set[str] = set()
    for t in seeds.get("terms") or []:
        for b in t.get("forbidden") or []:
            if b and b not in seen_needles:
                seen_needles.add(b)
                text_rules.append((b, "T1", f"出现「{b}」，标准写法「{t['standard']}」"
                                           f"——按术语锁定表替换"))
    for b in seeds.get("forbiddenNames") or []:
        if b and b not in seen_needles:
            seen_needles.add(b)
            text_rules.append((b, "T2", f"使用了明令禁用的称呼「{b}」——改回人物表登记的正式称呼"))
    for name, holders in (seeds.get("clashing") or {}).items():
        if name and name not in seen_needles:
            seen_needles.add(name)
            text_rules.append((name, "T2", f"「{name}」被 {('、'.join(holders))} 共用（同名不同人）"
                                          f"——请给其中一个换正式称呼"))

    for sc_id, b_idx, beat in _iter_beats(ep_data):
        for field in ("line", "action"):
            text = beat.get(field)
            if not isinstance(text, str) or not text:
                continue
            where = f"{label} 场 {sc_id} 拍 {b_idx}"
            for needle, tag, msg in text_rules:
                if needle in text:
                    bad.append(f"[{tag}] {where}：{msg}")

    # T2 人物层面：泛化命名 / 同名不同人（契约里 cast 存的是 C01 式编号，先映射回人名再判）
    registered = seeds.get("name_set") or set()
    clashing = seeds.get("clashing") or {}
    id_to_name = seeds.get("id_to_name") or {}
    for sc_idx, c in enumerate(_as_dict(ep_data).get("scenes") or []):
        if not isinstance(c, dict):
            continue
        sc_id = str(c.get("sceneId") or c.get("scene") or f"S{sc_idx + 1:02d}").strip()
        for who_raw in _as_str_list(c.get("characters")):
            who = id_to_name.get(who_raw, who_raw)
            if who in clashing:
                bad.append(f"[T2] {label} 场 {sc_id}：「{who}」被 {('、'.join(clashing[who]))} "
                           f"共用（同名不同人）——请给其中一个换正式称呼")
            elif who not in registered and _GENERIC_NAME_RE.match(who):
                bad.append(f"[T2] {label} 场 {sc_id}：出现泛化命名「{who}」，未在人物表登记"
                           f"——改为带姓氏的半具体名，或正式登记进人物表")

    return bad


def final_naming_sweep(script_merged: Any, seeds: Optional[Dict[str, Any]]) -> List[str]:
    """T1/T2 终扫：对汇编全剧逐集复检（与 F3 同场硬闸）。无种子返回空列表。"""
    if not seeds:
        return []
    bad: List[str] = []
    for ep in _as_dict(script_merged).get("episodes") or []:
        bad.extend(ep_level_checks(ep, seeds))
    return list(dict.fromkeys(bad))


def _beat_shape(beat_type: Any) -> str:
    """骨架归一：剥掉「第N集_」前缀（单元剧大纲的节拍类型是按集生成的，带前缀即永不雷同）。"""
    return _EP_PREFIX_RE.sub("", str(beat_type or "").strip())


def struct_isomorphism_warnings(outline_data: Any, episodes_states: Any = None) -> List[str]:
    """T3 结构雷同：相邻 ≥2 集节拍骨架同构（照搬 fbs B2-C 判据形态，域换集）。警告级。"""
    outline = _as_dict(outline_data)
    seq_by_ep: Dict[Any, List[str]] = {}

    if isinstance(episodes_states, list):
        for it in episodes_states:
            if not isinstance(it, dict):
                continue
            raw = it.get("beats") or it.get("beatsClaimed")
            if isinstance(raw, list) and raw:
                seq_by_ep[it.get("ep")] = [_beat_shape(b.get("type") if isinstance(b, dict) else b)
                                           for b in raw]

    order: List[Any] = [e.get("ep") for e in (outline.get("episodes") or []) if isinstance(e, dict)]
    if not seq_by_ep:
        for b in outline.get("beats") or []:
            if not isinstance(b, dict):
                continue
            shape = _beat_shape(b.get("type"))
            if shape:
                seq_by_ep.setdefault(b.get("episode"), []).append(shape)
        order = order or sorted([n for n in seq_by_ep if n is not None], key=str)
    order = [n for n in order if seq_by_ep.get(n)]

    warns: List[str] = []
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and seq_by_ep[order[j + 1]] == seq_by_ep[order[i]]:
            j += 1
        if j > i:
            shape_txt = "→".join(seq_by_ep[order[i]])
            warns.append(f"第 {order[i]}-{order[j]} 集骨架雷同（{shape_txt}）——连续同构会让观众看出套路，"
                         f"建议打散节拍顺序或换开场方式")
        i = j + 1
    return warns


def rhythm_stats(episodes: Any) -> Dict[str, Any]:
    """T4 节奏统计：动作行句长变异系数 + 破折号密度（照搬 fbs B2-A / S6 口径）。

    数据全产、判级仅提示——样本小时 CV 易误报，故只报告不设闸（Spec §七③）。
    """
    lengths: List[int] = []
    dashes = 0
    for ep in episodes or []:
        for _sc, _i, beat in _iter_beats(ep):
            act = beat.get("action")
            if not isinstance(act, str):
                continue
            for seg in _SENTENCE_SPLIT_RE.split(act):
                seg = seg.strip()
                if seg:
                    lengths.append(len(seg))
            dashes += act.count(_DASH)

    n = len(lengths)
    total_chars = sum(lengths)
    if n == 0 or total_chars == 0:
        return {"action_sentences": 0, "action_sentence_cv": 0.0,
                "dash_density_per_1k": 0.0, "total_chars": 0}
    mean = total_chars / n
    var = sum((x - mean) ** 2 for x in lengths) / n
    return {
        "action_sentences": n,
        "action_sentence_cv": round((var ** 0.5) / mean, 3),
        "dash_density_per_1k": round(dashes / total_chars * 1000, 2),
        "total_chars": total_chars,
    }


# ---------- 待回收清单派生（写手上下文包用） ----------

def foreshadow_todo_for_ep(states: Dict[str, Dict[str, Any]], ep_n: Any) -> List[Dict[str, Any]]:
    """本集按计划应当回收的伏笔清单（把待回收清单直接摆到写手面前）。"""
    todo: List[Dict[str, Any]] = []
    for fid in sorted(states or {}):
        st = _as_dict(states[fid])
        if st.get("status") != "open" or st.get("payoffPlannedEp") != ep_n:
            continue
        todo.append({"id": fid, "text": st.get("text", ""), "plantedInEp": st.get("plantedInEp"),
                     "payoffPlannedEp": ep_n})
    return todo


def foreshadow_open_overview(states: Dict[str, Dict[str, Any]]) -> List[str]:
    """全剧未回收伏笔概览（一行一条，供提示词与报告复用）。"""
    out: List[str] = []
    for fid in sorted(states or {}):
        st = _as_dict(states[fid])
        if st.get("status") != "open":
            continue
        planned = st.get("payoffPlannedEp")
        tail = (f"计划第 {planned} 集回收" if isinstance(planned, int) and not isinstance(planned, bool)
                else "尚未登记回收集")
        out.append(f"{fid}「{st.get('text', '')}」{tail}")
    return out


def register_paid_claims(workdir: Any, ep_data: Any, ep_n: Any,
                         states: Dict[str, Dict[str, Any]]) -> List[str]:
    """F2 认领对账：把本集声明兑现的伏笔合成 pay 事件，返回 ⚠ 警告清单。

    采信规则（照搬 fbs commitments「核销证明原文」必填 + 严禁高分掩盖）：
    - 认领的 ID 必须已在账本里（否则 ⚠ 不合成）；
    - 必须有场景证据（第几集第几场），缺证据 ⚠ 不合成；
    - 认领集早于埋设集 → ⚠ 不合成；晚于计划 1 集以上 → ⚠ 但按兑现采信。
    另附「超期未填」点名（计划回收集已过仍未认领）。
    ⚠ 一律只进报告、不进回炉指令——防模型为过闸硬塞假填坑。
    """
    states = states if isinstance(states, dict) else {}
    warns: List[str] = []
    raw = _as_dict(ep_data).get("foreshadowsPaid")
    if raw is None:
        claims: List[Any] = []
    elif isinstance(raw, list):
        claims = raw
    elif isinstance(raw, (str, dict)):
        claims = [raw]          # 模型偶发少包一层：归一后走同一条校验，绝不让认领凭空蒸发
    else:
        claims = []
        warns.append(f"foreshadowsPaid 格式不对（应为列表）：{type(raw).__name__}——本次不合成回收记录")
    claimed_now: Set[str] = set()

    for item in claims:
        if isinstance(item, str):
            fid, evidence = item.strip(), ""
        elif isinstance(item, dict):
            fid = str(item.get("id", "")).strip()
            evidence = str(item.get("evidence", "")).strip()
        else:
            continue
        if not fid:
            continue
        st = states.get(fid)
        if st is None:
            warns.append(f"伏笔 {fid} 认领无效：账本里没有这条伏笔（ID 不存在），本次不合成回收记录")
            continue
        if st.get("status") == "paid":
            warns.append(f"伏笔 {fid} 已在第 {st.get('paidInEp')} 集认领过，第 {ep_n} 集属重复认领"
                         f"——保留首次证据，不重复记账")
            continue
        if not evidence:
            warns.append(f"伏笔 {fid} 认领缺场景证据——补上「第几集第几场」的核销定位后重新认领，"
                         f"本次不合成回收记录")
            continue
        planted = st.get("plantedInEp")
        if isinstance(planted, int) and not isinstance(planted, bool) and ep_n < planted:
            warns.append(f"伏笔 {fid} 认领时机早于埋设（埋于第 {planted} 集，本集第 {ep_n} 集），"
                         f"本次不合成回收记录")
            continue
        planned = st.get("payoffPlannedEp")
        if isinstance(planned, int) and not isinstance(planned, bool) and ep_n > planned + 1:
            warns.append(f"伏笔 {fid} 认领晚于计划（计划第 {planned} 集回收，实际第 {ep_n} 集）"
                         f"——已按兑现采信，请确认是否顺延大纲计划")
        emit_event(workdir, EV_PAY, ep=ep_n, id=fid, evidence=evidence,
                   epHash=ep_content_hash(ep_data))
        claimed_now.add(fid)

    for fid in sorted(states):
        st = _as_dict(states[fid])
        if st.get("status") != "open" or fid in claimed_now:
            continue
        planned = st.get("payoffPlannedEp")
        if isinstance(planned, int) and not isinstance(planned, bool) and planned <= ep_n:
            desc = str(st.get("text") or "").strip()
            warns.append(f"伏笔 {fid}" + (f"「{desc}」" if desc else "")
                         + f"计划第 {planned} 集回收，到第 {ep_n} 集仍未认领（超期未填）")
    return warns


# ---------- 账本状态点名（F2/F3 报告底座，纯函数） ----------
def lint_foreshadow_states(states: Dict[str, Dict[str, Any]],
                           outline_data: Any = None,
                           completed_eps: Optional[Set[int]] = None,
                           final: bool = False) -> List[str]:
    """账本重放结果的点名清单：F2 认领对账 + F3 终验未清零。

    返回项以 `[F2]`/`[F3]` 前缀区分（警告 vs 硬闸）。`final=True` 即终验口径：
    未回收伏笔**一律**点名（全绿不豁免），不再只看是否超期。
    """
    bad: List[str] = []
    done = set(completed_eps or [])
    for fid in sorted(states):
        st = _as_dict(states.get(fid))
        status = st.get("status")
        desc = str(st.get("text") or "").strip()
        head = f"伏笔 {fid}" + (f"「{desc}」" if desc else "")
        planned = st.get("payoffPlannedEp")
        planned_ok = isinstance(planned, int) and not isinstance(planned, bool)
        tail = f"计划第 {planned} 集回收" if planned_ok else "尚未登记回收集"

        if status == "open":
            if final:
                bad.append(f"[F3] 未回收伏笔 {head}（{tail}）——埋了必须收，交付前不许留下未回收线索")
            elif planned_ok and planned in done:
                bad.append(f"[F2] {head} {tail}，该集已完成仍未认领（超期未填）")
        elif status == "paid":
            if not str(st.get("evidence") or "").strip():
                bad.append(f"[F2] {head} 已认领但缺场景证据——补上「第几集第几场」的核销定位")
        elif status == "abandoned":
            if not str(st.get("abandonReason") or "").strip():
                bad.append(f"[F3] {head} 已放弃但没写放弃原因——线索不得悄悄埋掉")
            elif st.get("decidedBy") != "user":
                bad.append(f"[F3] {head} 已放弃但未经用户拍板（decidedBy={st.get('decidedBy')}）"
                           f"——放弃伏笔是用户的决定，不是模型的")
    return bad


def final_foreshadow_sweep(workdir: Any, valid_eps: Optional[Set[int]] = None,
                           outline_data: Any = None,
                           ep_hashes: Optional[Dict[int, str]] = None) -> List[str]:
    """F3 终验硬闸：未回收必须清零（abandoned 须有原因且经用户拍板）。

    **全绿不豁免**：调用点（run_batch 终验序）不看 12 门结果，直接执行本函数；
    返回非空即交付未过、exit 1 点名。传 ep_hashes 时按内容指纹对账，防止重生成后的旧认领复活。
    """
    states = replay(workdir, valid_eps, ep_hashes)
    return [v for v in lint_foreshadow_states(states, outline_data, valid_eps, final=True)
            if v.startswith("[F3]")]


# ---------- CLI ----------

def _load_cached_episodes(workdir: Any) -> List[Dict[str, Any]]:
    """装载工作区里全部已过审单集缓存（坏文件跳过）。"""
    eps_dir = os.path.join(os.path.abspath(str(workdir)), "state", "episodes")
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(eps_dir):
        return out
    for fn in sorted(os.listdir(eps_dir)):
        if not (fn.startswith("ep") and fn.endswith(".json")):
            continue
        try:
            with open(os.path.join(eps_dir, fn), "r", encoding="utf-8") as fp:
                ep = json.load(fp)
        except (OSError, ValueError):
            continue
        if isinstance(ep, dict):
            ep.pop("_meta", None)
            out.append(ep)
    return out


def _gate(ok: bool, gid: str, label: str, detail: str = "") -> Dict[str, Any]:
    return {"id": gid, "label": label, "ok": bool(ok), "detail": detail}


def _split_by_prefix(items: List[str], prefix: str) -> List[str]:
    return [v for v in items if v.startswith(prefix)]


def build_report(workdir: Any, outline_data: Any = None, art_data: Any = None) -> Dict[str, Any]:
    """组装三键报告：foreshadow_gates / naming_gates / rhythm（不碰 validate 的 gates 键）。"""
    episodes = _load_cached_episodes(workdir)
    ep_hashes = {ep.get("ep"): ep_content_hash(ep) for ep in episodes
                 if isinstance(ep.get("ep"), int)}
    valid_eps = set(ep_hashes)          # 有效集就是指纹底账的键集，两处口径必须同源
    states = replay(workdir, valid_eps, ep_hashes)
    seeds = build_naming_seeds(outline_data, art_data)

    # --- F 族 ---
    fs_items: List[str] = lint_foreshadow_states(states, outline_data, valid_eps)
    f2 = _split_by_prefix(fs_items, "[F2]")
    f3 = _split_by_prefix(fs_items, "[F3]")
    foreshadow_gates: List[Dict[str, Any]] = []
    if outline_data is not None:
        f1 = lint_registration(_as_dict(outline_data))
        foreshadow_gates.append(_gate(not f1, "F1", "伏笔登记合法（回收集存在 / ID 唯一 / 文本非空）",
                                      "；".join(v.replace("[F1] ", "") for v in f1)))
    foreshadow_gates.append(_gate(not f2, "F2", "伏笔认领对账（证据齐全、认领时机合理）",
                                  "；".join(v.replace("[F2] ", "") for v in f2)))
    foreshadow_gates.append(_gate(not f3, "F3", "终验未回收清零（放弃须有原因且经用户拍板）",
                                  "；".join(v.replace("[F3] ", "") for v in f3)))
    n_open = sum(1 for s in states.values() if s.get("status") == "open")
    n_paid = sum(1 for s in states.values() if s.get("status") == "paid")
    n_give = sum(1 for s in states.values() if s.get("status") == "abandoned")
    foreshadow_gates.append(_gate(True, "F-总览", "伏笔总览",
                                  f"共 {len(states)} 条：未回收 {n_open}／已回收 {n_paid}／已放弃 {n_give}"))

    # --- T 族 ---
    if seeds is None:
        skip = "跳过（未提供人物/美术种子）"
        naming_gates = [_gate(True, gid, label, skip) for gid, label in (
            ("T1", "术语锁定（标准写法 / 禁用变体）"),
            ("T2", "人物命名冲突与泛化命名"),
        )]
    else:
        hits: List[str] = []
        for ep in episodes:
            hits.extend(ep_level_checks(ep, seeds))
        t1 = _split_by_prefix(hits, "[T1]")
        t2 = _split_by_prefix(hits, "[T2]")
        naming_gates = [
            _gate(not t1, "T1", "术语锁定（标准写法 / 禁用变体）",
                  "；".join(v.replace("[T1] ", "") for v in t1)),
            _gate(not t2, "T2", "人物命名冲突与泛化命名",
                  "；".join(v.replace("[T2] ", "") for v in t2)),
        ]
    iso = struct_isomorphism_warnings(outline_data, episodes)
    naming_gates.append(_gate(True, "T3", "结构雷同（相邻 ≥2 集骨架同构，警告级）", "；".join(iso)))

    # 注册表层提示：人物表自身登记了「小X/大X/老X」泛化命名（照搬 fbs 命名规范，仅提示不否决）
    generic_registered = [n for n in (seeds or {}).get("names", []) if _GENERIC_NAME_RE.match(str(n))]
    naming_gates.append(_gate(
        True, "T2-注册", "人物表命名规范（泛化命名提示）",
        "人物表登记了泛化命名 " + "、".join(f"「{n}」" for n in generic_registered)
        + "——建议改为带姓氏的半具体名" if generic_registered else "人物表命名规范通过"))

    st = rhythm_stats(episodes)
    rhythm = [_gate(True, "T4", "动作行句长节奏与标点纪律（提示级）",
                    f"动作行 {st['action_sentences']} 句，句长变异系数 {st['action_sentence_cv']}"
                    f"（参考 ≥0.3），破折号密度 {st['dash_density_per_1k']}/千字"
                    f"；样本小易误报，仅提示不设闸")]

    return {"foreshadow_gates": foreshadow_gates, "naming_gates": naming_gates, "rhythm": rhythm}


def _print_section(title: str, items: List[Dict[str, Any]]) -> None:
    print(f"\n【{title}】")
    for g in items:
        mark = "✓" if g["ok"] else "✗"
        print(f"  {mark} {g['label']}")
        if g["detail"]:
            for line in str(g["detail"]).split("；"):
                if line.strip():
                    print(f"      ↳ {line.strip()}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dramacraft.continuity_lint",
        description="创作纵深哨兵：伏笔账本（F 族）与命名节奏（T 族）白话体检")
    parser.add_argument("workdir", help="运行期工作区（读 state/episodes 与 state/foreshadow_log.jsonl）")
    parser.add_argument("--outline", help="大纲 outline.json（提供后启用术语/结构同构检查与 F1）")
    parser.add_argument("--art", help="美术设定 art.json（提供后场景/道具名并入术语锁定表）")
    parser.add_argument("--json", dest="json_path", help="结构化报告落盘路径（禁解析终端混流）")
    parser.add_argument("--abandon", metavar="FID",
                        help="登记放弃某条伏笔（须配合 --reason；放弃是用户的决定，会记 decidedBy=user）")
    parser.add_argument("--reason", help="放弃原因（--abandon 必填，白话一句话）")
    args = parser.parse_args(argv)

    if args.abandon:
        if not str(args.reason or "").strip():
            print(f"❌ --abandon {args.abandon} 必须附 --reason：放弃伏笔要写明原因，线索不得悄悄埋掉。")
            return 1
        if args.abandon not in replay(args.workdir, None):
            print(f"❌ 伏笔 {args.abandon} 不在账本里，无法放弃。先用报告确认账本里的伏笔 ID。")
            return 1
        emit_event(args.workdir, EV_ABANDON, id=args.abandon,
                   abandonReason=str(args.reason).strip(), decidedBy="user")
        print(f"✓ 已登记放弃伏笔 {args.abandon}（用户拍板）：{str(args.reason).strip()}")

    def _load(p):
        if not p:
            return None
        if not os.path.exists(p):
            print(f"❌ 文件不存在: {p}")
            return {}
        with open(p, "r", encoding="utf-8") as fp:
            return json.load(fp)

    report = build_report(args.workdir, _load(args.outline), _load(args.art))

    print("================== 创作纵深哨兵报告（F 族 + T 族）==================")
    _print_section("伏笔账本", report["foreshadow_gates"])
    _print_section("命名与结构", report["naming_gates"])
    _print_section("节奏", report["rhythm"])
    print("================================================================")

    if args.json_path:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_path)), exist_ok=True)
        with open(args.json_path, "w", encoding="utf-8") as fp:
            json.dump(report, fp, ensure_ascii=False, indent=2)

    blocked = [g for k, v in report.items() for g in v if not g["ok"]]
    if blocked:
        print(f"\n❌ 共 {len(blocked)} 项未通过，请按上述定位修正。")
        return 1
    print("\n✓ 哨兵检查全部通过。")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
