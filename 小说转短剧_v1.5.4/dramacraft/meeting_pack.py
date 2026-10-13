"""会议机读件（v1.5.0 T6；方案 §4 + fbs session-protocols v2.1.2）。

把「人读的会议规范」与「机器要核的账」接起来，四件事：

1. **简报**（`build_brief/write_brief`）：任何会议前先产出约 500 字简报（原件 :25-38 的模板逐字段照抄），
   参会方只读简报、不读全部项目文件；关键背景 ≤300 字，超长截断并标「…（截断）」。
2. **关切合并**（`merge_concerns`）：多位参会方的关切按文本归一化后的 Jaccard ≥0.6 并条，票数累加，
   **不得丢票**（丢票 = 观众声音被吞）。
3. **评审分级**（`grade_review`）：🔴严重 → P0、🟡中等 → P1、⚪轻微 → P2；**命中红线（R1-R4）一律 P0**，
   与监督卡「红线即严重」同口径（生产件里 P0 会拦交付）。
4. **纪要与闭环**（`append_minutes` / `append_concerns` / `decide_concern` / `reader_closure_open`）：
   纪要按原件五字段强校验后追加落盘；读者会每条关切必须有决议行（接受/部分接受/驳回）才算闭环。

口径订正（2026-09-22，按原件而非转述）：纪要字段是**五个**（核心结论 / 关键决策 / 被放弃的方向 /
待人类确认的问题 / 下一步）+ 会议类型 + 日期（原件 :60-72），不是工单初稿写的「九字段」。

落盘位置：`<workdir>/state/meetings/`
- `brief-<kind>.md`｜`minutes-<kind>.md`（追加）
- `concerns-<kind>.jsonl`｜`decisions-<kind>.jsonl`（append-only）

CLI::

    python -m dramacraft.meeting_pack check-closure  --workdir W [--kind reader] --json R
        退出码 0 = 读者关切全闭环；4 = 有未闭环；1 = 用法错或输入不可用（不落报告）
    python -m dramacraft.meeting_pack sync-concerns  --workdir W --inputs a|b|c --json R
        并条（merge_concerns）后落盘：同一句沿用原 id、新关切领新 id，重跑不追加；退出码 0 / 1
    python -m dramacraft.meeting_pack decide-concern --workdir W --id RC1 --decision 接受 --json R
        落一条决议（id 必须在清单里，否则拒收）；退出码 0 / 1
    python -m dramacraft.meeting_pack grade-review   --inputs a|b --json R
        评审分级（🔴严重与命中红线一律 P0）；退出码 0 / 1
    python -m dramacraft.meeting_pack minutes-reader --workdir W --merged R --json R2
        读者会五字段纪要（按稳定 id 配对）；退出码 0 / 1（纪要没落齐即 1）
    python -m dramacraft.meeting_pack minutes-review --workdir W --graded R --strength S --p0-decision D --json R2
        评审会五字段纪要（会议强度 + P0 的真实处置）；退出码 0 / 1
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

MINUTES_FIELDS: Tuple[str, ...] = (
    "核心结论", "关键决策", "被放弃的方向", "待人类确认的问题", "下一步",
)

MEETING_KINDS: Dict[str, str] = {
    "reader": "读者会",
    "review": "评审会",
    "adversarial": "对抗会",
}

# P0/P1/P2 口径（内嵌单源；监督卡「红线即严重」在此落地）
_SEVERITY_TO_GRADE = {
    "🔴": "P0", "🔴 严重": "P0", "严重": "P0",
    "🟡": "P1", "🟡 中等": "P1", "中等": "P1",
    "⚪": "P2", "⚪ 轻微": "P2", "轻微": "P2",
}
# 查表用去空格版：「🔴严重」这种中文书写（emoji 后不带空格）必须照样进 P0
_NORMALIZED_SEVERITY = {k.replace(" ", ""): v for k, v in _SEVERITY_TO_GRADE.items()}
_DECISIONS = ("接受", "部分接受", "驳回")

_BRIEF_TOTAL_CAP = 620          # 整篇上限（测试与调用方都按这个数）
_BRIEF_BACKGROUND_CAP = 300     # 关键背景上限（原件 :25-38）

# 并条阈值：归一（去空白与标点）后字符集合的 Jaccard。
# 取 0.8 是精度优先：0.6 会把语义相反的关切并成一条并翻倍票数
# （评审实测「节奏太慢/节奏太乱」「主角太弱/主角太强」相似度都是 0.6）。
# 宁多报不漏报——两条相似关切分开显示，比把相反意见糊成一条好。
JACCARD_THRESHOLD = 0.8


class MeetingInputError(Exception):
    """输入件不可用——CLI exit 1（不落报告）。"""


# ------------------------------------------------------------------ 简报

def build_brief(meeting: str, title: str, goal: str, background: str,
                status: str = "", genre: str = "") -> str:
    """按原件模板产出会议简报（字段顺序与名称照抄 :25-38）。"""
    kind = MEETING_KINDS.get(str(meeting))
    if kind is None:
        raise ValueError(f"未知会议类型：{meeting}（可用：{'、'.join(MEETING_KINDS)}）")
    bg = str(background or "").strip()
    if len(bg) > _BRIEF_BACKGROUND_CAP:
        bg = bg[:_BRIEF_BACKGROUND_CAP] + "…（截断）"

    def _cap(text: Any, limit: int) -> str:
        t = str(text or "").strip()
        return t if len(t) <= limit else t[:limit] + "…（截断）"

    # 每行先各自封顶，再按"总上限减掉其余固定行"分配背景长度：
    # 整篇一定不超上限，且截断永远落在背景这一行（不会把字段名切成半截）
    head = [
        f"━━ 会议简报 · {kind} ━━",
        f"剧名：{_cap(title, 60)}",
        f"主题：{_cap(goal, 80)}",
        f"体裁与读者：{_cap(genre or '短剧观众', 40)}",
        f"当前状态：{_cap(status or '产线运行中', 60)}",
        f"本次目标：{_cap(goal, 80)}",
    ]
    fixed = len("\n".join(head)) + len("\n") + len("关键背景（≤300字）：") + len("\n━━━━━━━━━━━━━━━━━━")
    bg_budget = max(40, min(_BRIEF_BACKGROUND_CAP, _BRIEF_TOTAL_CAP - fixed))
    brief = "\n".join(head + [f"关键背景（≤300字）：{_cap(bg, bg_budget)}"] + ["━━━━━━━━━━━━━━━━━━"])
    return brief


def _meetings_dir(workdir: Any) -> Path:
    return Path(str(workdir)) / "state" / "meetings"


def write_brief(workdir: Any, kind: str, text: str) -> Path:
    if kind not in MEETING_KINDS:
        raise ValueError(f"未知会议类型：{kind}")
    path = _meetings_dir(workdir) / f"brief-{kind}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(text), encoding="utf-8")
    return path


# ------------------------------------------------------------------ 关切合并

def _concern_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("t", "text", "title", "关切"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val
    return ""


_PUNCT = set("，。！？；：、,.!?;:'\"“”‘’()（）[]【】<>《》—…-·~ ")


def _normalize(text: str) -> str:
    """去空白与标点：只差标点/空格的两条算同一条（语义差异一律保留）。"""
    return "".join(ch for ch in str(text) if not ch.isspace() and ch not in _PUNCT)


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(_normalize(a)), set(_normalize(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def merge_concerns(raws: Iterable[Iterable[Any]], threshold: float = JACCARD_THRESHOLD) -> List[Dict[str, Any]]:
    """把多位参会方的关切并条（Jaccard ≥ threshold 视为同一条），**票数累加不丢票**。"""
    groups: List[Dict[str, Any]] = []
    flat = [it for raw_list in (raws or []) for it in (raw_list or [])]
    dropped = sum(1 for it in flat if not _concern_text(it).strip())
    for item in flat:
        text = _concern_text(item)
        if not text.strip():
            continue
        hit = next((g for g in groups if _jaccard(g["t"], text) >= threshold), None)
        if hit is None:
            groups.append({"t": text, "votes": 1, "merged": [item]})
        else:
            hit["votes"] += 1
            hit["merged"].append(item)
    if dropped:
        raise ValueError(f"有 {dropped} 条关切读不出正文（缺 t/text 字段）——先让参会方按契约写，"
                         f"少了这些条目会议结论就是残缺的")
    groups.sort(key=lambda g: -g["votes"])          # 票高在前；同票保持出现顺序（stable sort）
    return groups


# ------------------------------------------------------------------ 评审分级

def grade_review(items: Iterable[Any]) -> Dict[str, List[Dict[str, Any]]]:
    """按严重程度分 P0/P1/P2；带 `redline`（R1-R4）的一律进 P0。"""
    out: Dict[str, List[Dict[str, Any]]] = {"P0": [], "P1": [], "P2": []}
    for item in items or []:
        rec = dict(item) if isinstance(item, dict) else {"t": str(item)}
        redline = str(rec.get("redline") or "").strip().upper()
        severity = str(rec.get("severity") or "").replace(" ", "").strip()
        grade = "P0" if redline.startswith("R") and redline[1:2].isdigit() else \
            _NORMALIZED_SEVERITY.get(severity, "P1")
        out[grade].append(rec)
    return out


# ------------------------------------------------------------------ 纪要（五字段）

def append_minutes(workdir: Any, kind: str, minutes: Dict[str, Any]) -> Path:
    """按原件五字段强校验后追加纪要；缺字段/字段名不对即拒收（宁可报错也不落半截纪要）。"""
    if kind not in MEETING_KINDS:
        raise ValueError(f"未知会议类型：{kind}（可用：{'、'.join(MEETING_KINDS)}）")
    if not isinstance(minutes, dict):
        raise ValueError("纪要必须是字典（五字段）")
    missing = [f for f in MINUTES_FIELDS if f not in minutes]
    if missing:
        raise ValueError(f"纪要缺字段：{'、'.join(missing)}（原件共用尾部格式就是这五个字段）")

    def _render(val: Any) -> str:
        if val is None:
            return "无"
        if isinstance(val, (list, tuple)):
            items = [str(x).strip() for x in val if str(x or "").strip()]
            return "；".join(items) if items else "无"
        text = str(val).strip()
        return text if text and text != "None" else "无"

    date = str(minutes.get("日期") or datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d"))
    block = [f"━━ 会议纪要 · {MEETING_KINDS[kind]} · {date} ━━"]
    for field in MINUTES_FIELDS:
        block.append(f"{field}：{_render(minutes[field])}")
    block.append("━━━━━━━━━━━━━━━━━━")
    path = _meetings_dir(workdir) / f"minutes-{kind}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lead = "\n" if path.exists() and path.stat().st_size > 0 else ""
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(lead + "\n".join(block) + "\n")
    return path


# ------------------------------------------------------------------ 读者关切闭环

def _append_jsonl(path: Path, records: Iterable[Dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "a", encoding="utf-8") as fp:
        for rec in records or []:
            fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return n


def _next_free_id(used: Iterable[str]) -> str:
    """下一个没被占用的 RC 号（不按数组下标推算：重跑要落到同一个号上）。"""
    taken = set(used)
    seq = 1
    while f"RC{seq}" in taken:
        seq += 1
    return f"RC{seq}"


def append_concerns(workdir: Any, concerns: Iterable[Dict[str, Any]], kind: str = "reader") -> int:
    """落关切条目（append-only）。缺 id 的按 RC1、RC2… 顺序补。"""
    if kind not in MEETING_KINDS:
        raise ValueError(f"未知会议类型：{kind}")
    path = _meetings_dir(workdir) / f"concerns-{kind}.jsonl"
    existing = {c.get("id") for c in _load_jsonl(path)}
    recs = []
    for item in concerns or []:
        rec = dict(item) if isinstance(item, dict) else {"t": str(item)}
        if rec.get("id"):
            if str(rec["id"]) in existing:      # 显式 id 也要查重：一条决议只能关一条关切
                raise ValueError(f"关切 id 重复：{rec['id']}")
            existing.add(str(rec["id"]))
            recs.append(rec)
            continue
        rec["id"] = _next_free_id(existing)
        existing.add(rec["id"])
        recs.append(rec)
    return _append_jsonl(path, recs)


def sync_reader_concerns(workdir: Any, merged: Iterable[Any], kind: str = "reader") -> List[Dict[str, Any]]:
    """把本轮合并后的关切同步进工作区：**同一句关切沿用原 ID**，新关切领新 ID，只追增不重写。

    历史匹配只认归一文本（去空白与标点）——语义相近的并条归 `merge_concerns` 管，
    这里不做相似度判断，免得把「节奏太慢」和「节奏太乱」这类相反意见并成一条。
    返回本轮合并件（每条带稳定 `id`）：工作流按这个 id 提请决议、写纪要、核闭环，
    重跑时已决议的项不会被重问，也不会被翻回未决议。
    """
    if kind not in MEETING_KINDS:
        raise ValueError(f"未知会议类型：{kind}")
    path = _meetings_dir(workdir) / f"concerns-{kind}.jsonl"
    known = _load_jsonl(path, strict=True)          # 坏行即报错：读不出来就会重复追问
    by_text: Dict[str, str] = {}
    used = set()
    for row in known:
        cid = str(row.get("id") or "")
        if cid:
            used.add(cid)
            by_text.setdefault(_normalize(_concern_text(row)), cid)

    def _votes(item: Any) -> int:
        if not isinstance(item, dict):
            return 1
        try:
            return max(1, int(item.get("votes", 1)))
        except (TypeError, ValueError):
            return 1

    rows: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    for item in merged or []:
        text = _concern_text(item)
        if not text.strip():
            raise ValueError("有 1 条关切读不出正文（缺 t/text 字段）——整批不落，先让参会方按契约写")
        key = _normalize(text)
        if key in seen:                              # 同批同句：并成一条，票数累加（不许丢票）
            seen[key]["votes"] = int(seen[key].get("votes") or 0) + _votes(item)
            continue
        row: Dict[str, Any] = dict(item) if isinstance(item, dict) else {"t": str(item)}
        row["t"] = text
        row["votes"] = _votes(item)
        cid = by_text.get(key)
        if cid:
            row["id"] = cid                          # 老工作区里这句已有 id：沿用（决议跟着走）
        else:
            row["id"] = _next_free_id(used)
            used.add(row["id"])
            by_text[key] = row["id"]
            pending.append(row)                      # 只有新关切才追增
        seen[key] = row
        rows.append(row)
    _append_jsonl(path, pending)                     # 先全部校验完再落：异常不留半批记录
    if not path.exists():
        # 会开过但一条关切都没有也要留下空清单：文件在 = 「开过会、没有关切」，
        # 文件不在 = 「会没开」。少了这个区别，零关切的会会被闭环核账判成「读不出来」。
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return rows


def decide_concern(workdir: Any, cid: str, decision: str, reason: str = "",
                   kind: str = "reader", decided_by: str = "user") -> Dict[str, Any]:
    """给一条关切落决议行（接受/部分接受/驳回）。结论必须落在这三档里，不许含糊。"""
    if kind not in MEETING_KINDS:
        raise ValueError(f"未知会议类型：{kind}（可用：{'、'.join(MEETING_KINDS)}）")
    verdict = next((d for d in _DECISIONS if str(decision).strip() == d), None)   # 精确匹配：前缀匹配会把"接受不了"算成"接受"
    if verdict is None:
        raise ValueError(f"决议只能是「{'/'.join(_DECISIONS)}」三档之一，收到：{decision}")
    rec = {"id": str(cid), "decision": verdict, "reason": str(reason or ""),
           "decidedBy": decided_by, "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")}
    _append_jsonl(_meetings_dir(workdir) / f"decisions-{kind}.jsonl", [rec])
    return rec


def _load_jsonl(path: Path, strict: bool = False) -> List[Dict[str, Any]]:
    """读 append-only 清单。strict=True 时坏行即报错——静默跳过会让未闭环关切凭空消失。"""
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out
    bad = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(doc, dict):
            out.append(doc)
    if strict and bad:
        raise MeetingInputError(f"清单里有 {bad} 行读不出来（{path}）——先修好人再核闭环")
    return out


def reader_closure_open(workdir: Any, kind: str = "reader") -> List[Dict[str, Any]]:
    """还没落决议行的关切（未闭环）。只认这一种会的关切清单。"""
    concerns_path = _meetings_dir(workdir) / f"concerns-{kind}.jsonl"
    if not concerns_path.exists():
        raise MeetingInputError(f"{MEETING_KINDS.get(kind, kind)}还没开过（没有关切清单：{concerns_path}）")
    concerns = _load_jsonl(concerns_path, strict=True)
    decided = {d.get("id") for d in _load_jsonl(_meetings_dir(workdir) / f"decisions-{kind}.jsonl", strict=True)}
    return [c for c in concerns if c.get("id") not in decided]


def reader_guidance(workdir: Any, current_rows: Iterable[Any], kind: str = "reader") -> str:
    """把「本轮关切里已接受/部分接受」的决议重建成写作指导（复跑不丢上一轮定下的改法）。

    真跑实测缺口（2026-09-23）：复跑时旧决议不再重问，但它们也没再进写手指导——于是第二稿
    把上一轮已经接受的改法又丢掉了。ID 稳定不等于指导稳定，指导必须按 ID 重建。

    - 关切清单与决议清单都严格读：坏行即报错，不静默跳过（跳过的可能是唯一一条有效指导）
    - 同一条关切出现结论矛盾的决议行 → 失败关闭，不任意取第一条
    - 驳回项不进指导；不在本轮关切集合里的历史决议也不进
    - 无有效指导返回空串
    """
    if kind not in MEETING_KINDS:
        raise ValueError(f"未知会议类型：{kind}（可用：{'、'.join(MEETING_KINDS)}）")
    meetings = _meetings_dir(workdir)
    known = {str(c.get("id")): c for c in _load_jsonl(meetings / f"concerns-{kind}.jsonl", strict=True)
             if c.get("id")}
    by_id: Dict[str, Dict[str, Any]] = {}
    for d in _load_jsonl(meetings / f"decisions-{kind}.jsonl", strict=True):
        cid = str(d.get("id") or "")
        if not cid:
            raise MeetingInputError("决议清单里有一条没写关切号——配不上对的决议不能当指导，先补 id")
        prev = by_id.get(cid)
        if prev is None:
            by_id[cid] = d
            continue
        if str(prev.get("decision")) != str(d.get("decision")):
            raise MeetingInputError(
                f"关切 {cid} 有两条结论冲突的决议（{prev.get('decision')} / {d.get('decision')}）"
                "——先把结论定下来再写稿，不替人挑一条")
        if not str(prev.get("reason") or "").strip() and str(d.get("reason") or "").strip():
            by_id[cid] = d                  # 结论一致：补上后来写的理由

    lines: List[str] = []
    for row in current_rows or []:
        cid = str((row or {}).get("id") or "") if isinstance(row, dict) else ""
        if not cid:
            continue
        decision = by_id.get(cid)
        if not decision or str(decision.get("decision")) not in ("接受", "部分接受"):
            continue
        text = _concern_text(known.get(cid) or row)
        if not text.strip():
            continue
        reason = str(decision.get("reason") or "").strip()
        lines.append(f"- {text}" + (f"（改法：{reason}）" if reason else ""))
    return "\n".join(lines)


# ------------------------------------------------------------------ 纪要组装（口径在 python 一处）

def build_reader_minutes(rows: Iterable[Dict[str, Any]], decisions: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """读者会五字段纪要：关切与决议**按稳定 ID 配对**，不按数组下标推算。"""
    items = [r for r in (rows or [])]
    by_id = {str(d.get("id")): d for d in (decisions or [])}
    paired = [(r, by_id[str(r.get("id"))]) for r in items if str(r.get("id")) in by_id]
    ok = [x for x in paired if x[1].get("decision") == "接受"]
    part = [x for x in paired if x[1].get("decision") == "部分接受"]
    no = [x for x in paired if x[1].get("decision") == "驳回"]
    unanswered = [r for r in items if str(r.get("id")) not in by_id]
    return {
        "核心结论": f"写作前读者会：本轮合并去重后 {len(items)} 条关切，逐条人判 {len(paired)} 条"
                    f"（接受 {len(ok)} / 部分接受 {len(part)} / 驳回 {len(no)}），未决议 {len(unanswered)} 条。",
        "关键决策": [f"{d.get('id')}：{d.get('decision')}——{d.get('reason') or '（未补理由）'}" for _, d in paired]
                    or ["（本次没有决议行）"],
        "被放弃的方向": [f"{d.get('id')}：{str(r.get('t') or '')}" for r, d in no] or ["无"],
        "待人类确认的问题": [f"未决议 {len(unanswered)} 条，前几条："
                             f"{'；'.join(str(r.get('t') or '') for r in unanswered[:5])}"] if unanswered else ["无"],
        "下一步": ["接受与部分接受的条目已随写手提示词进本批成稿；未决议的在终验前补齐决议即恢复全绿交付戳。"],
    }


def build_review_minutes(graded: Dict[str, Any], strength: str, p0_decision: str) -> Dict[str, Any]:
    """评审会五字段纪要：分级件 + 会议强度（心理契约）+ **P0 的真实处置**都要落在纸面上。"""
    g = graded if isinstance(graded, dict) else {}
    p0 = [x for x in (g.get("P0") or []) if isinstance(x, dict)]
    p1 = [x for x in (g.get("P1") or []) if isinstance(x, dict)]
    p2 = [x for x in (g.get("P2") or []) if isinstance(x, dict)]

    def _line(x: Dict[str, Any]) -> str:
        return f"{str(x.get('t') or '')}｜{str(x.get('where') or '')}｜建议：{str(x.get('advice') or '')}"

    return {
        "核心结论": f"评审会（强度：{str(strength or '未记录')}）：共 {len(p0) + len(p1) + len(p2)} 条问题，"
                    f"P0 {len(p0)} / P1 {len(p1)} / P2 {len(p2)}。交付处置：{str(p0_decision or '未记录')}",
        "关键决策": [f"P0 {len(p0)} 条全部记入运行报告，按条整改"] if p0 else ["没有 P0 硬伤，交付不卡板"],
        "被放弃的方向": ["无"],
        "待人类确认的问题": [_line(x) for x in p0][:8] or ["无"],
        "下一步": ["下一版按 P0 清单逐条改；P1/P2 进回炉清单。"],
    }


def minutes_landed(workdir: Any, kind: str) -> Dict[str, Any]:
    """回读刚写的纪要，核五字段是否真的在盘上（核不过就是没落成，调用方按失败处理）。"""
    path = _meetings_dir(workdir) / f"minutes-{kind}.md"
    if not path.exists():
        return {"path": str(path), "landed": False, "missing": list(MINUTES_FIELDS)}
    text = path.read_text(encoding="utf-8")
    missing = [f for f in MINUTES_FIELDS if f"{f}：" not in text]
    return {"path": str(path), "landed": not missing, "missing": missing, "chars": len(text)}


# ------------------------------------------------------------------ CLI

class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # 用法错一律 exit 1（与门族合同一致）
        self.print_usage(sys.stderr)
        print(f"meeting_pack: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def _read_json_file(path: Any) -> Any:
    try:
        with open(str(path), encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError) as err:
        raise MeetingInputError(f"输入件读不出来：{path}（{err}）")


def _write_report(path: Any, payload: Any) -> None:
    Path(str(path)).write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
                               encoding="utf-8")


def _merged_rows(path: Any) -> List[Dict[str, Any]]:
    """读本轮合并件：落盘形状是「每行带稳定 id 的数组」，也认 {"rows": [...]} 的包壳写法。"""
    doc = _read_json_file(path)
    rows = doc.get("rows") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise MeetingInputError(f"合并件结构无法识别（应为数组或含 rows 的对象）：{path}")
    return [r for r in rows if isinstance(r, dict)]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = _Parser(prog="python -m dramacraft.meeting_pack",
                 description="会议机读件：并条落盘、五字段纪要、读者会闭环核账")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check-closure", help="核读者会关切闭环（有未闭环即 exit 4）")
    p.add_argument("--workdir", required=True)
    p.add_argument("--kind", default="reader")
    p.add_argument("--json", required=True, dest="json_path", help="报告保存路径（JSON）")

    p_sync = sub.add_parser("sync-concerns", help="并条后落盘：同一句沿用原 id，新关切领新 id")
    p_sync.add_argument("--workdir", required=True)
    p_sync.add_argument("--inputs", required=True, help="各参会方的关切件路径，多个用 | 分隔")
    p_sync.add_argument("--kind", default="reader")
    p_sync.add_argument("--json", required=True, dest="json_path", help="本轮合并件保存路径（JSON，带稳定 id）")

    p_rm = sub.add_parser("minutes-reader", help="写读者会五字段纪要（按稳定 id 配对，不按下标推算）")
    p_rm.add_argument("--workdir", required=True)
    p_rm.add_argument("--merged", required=True, help="本轮合并件路径（带稳定 id）")
    p_rm.add_argument("--kind", default="reader")
    p_rm.add_argument("--json", required=True, dest="json_path", help="纪要结果保存路径（JSON）")

    p_rv = sub.add_parser("minutes-review", help="写评审会五字段纪要（含会议强度与 P0 的真实处置）")
    p_rv.add_argument("--workdir", required=True)
    p_rv.add_argument("--graded", required=True, help="评审分级件路径（grade_review 的产物）")
    p_rv.add_argument("--strength", required=True, help="会议强度（心理契约原文）")
    p_rv.add_argument("--p0-decision", required=True, dest="p0_decision",
                      help="P0 的真实处置；没有 P0 就写「无 P0」")
    p_rv.add_argument("--kind", default="review")
    p_rv.add_argument("--json", required=True, dest="json_path", help="纪要结果保存路径（JSON）")

    p_gr = sub.add_parser("grade-review", help="按严重程度与红线分级（🔴→P0、🔴红线一律 P0）")
    p_gr.add_argument("--inputs", required=True, help="各评审方的意见件路径，多个用 | 分隔")
    p_gr.add_argument("--json", required=True, dest="json_path", help="分级件保存路径（JSON）")

    p_dc = sub.add_parser("decide-concern", help="给一条关切落决议行（按稳定 id 配对）")
    p_dc.add_argument("--workdir", required=True)
    p_dc.add_argument("--id", required=True, dest="cid", help="关切 id（必须是这份清单里已有的）")
    p_dc.add_argument("--decision", required=True, help="接受 / 部分接受 / 驳回")
    p_dc.add_argument("--reason", default="", help="一句理由（驳回必给）")
    p_dc.add_argument("--kind", default="reader")
    p_dc.add_argument("--json", required=True, dest="json_path", help="决议行保存路径（JSON）")

    p_rg = sub.add_parser("reader-guidance", help="按稳定 id 重建写作指导（复跑时上一轮已接受的改法照样生效）")
    p_rg.add_argument("--workdir", required=True)
    p_rg.add_argument("--merged", required=True, help="本轮合并件路径（带稳定 id）")
    p_rg.add_argument("--kind", default="reader")
    p_rg.add_argument("--json", required=True, dest="json_path", help="写作指导保存路径（JSON）")

    args = ap.parse_args(list(argv) if argv is not None else None)

    try:
        kind = getattr(args, "kind", None)          # 分级子命令没有会种，其余一律先查会种
        if kind is not None and kind not in MEETING_KINDS:
            raise MeetingInputError(f"未知会议类型：{kind}")

        if args.cmd == "check-closure":
            open_items = reader_closure_open(args.workdir, args.kind)
            payload = {"kind": args.kind, "open": open_items, "openCount": len(open_items)}
            _write_report(args.json_path, payload)
            if open_items:
                print(f"✗ 还有 {len(open_items)} 条观众关切没有决议（接受/部分接受/驳回都得给一句）：")
                for c in open_items:
                    print(f"   待决议 {c.get('id')}：{c.get('t') or c.get('text') or ''}")
                return 4
            print("✓ 观众关切条条有决议，这一环闭环了")
            return 0

        if args.cmd == "grade-review":
            paths = [x for x in str(args.inputs).split("|") if x]
            if not paths:
                raise MeetingInputError("--inputs 至少要给一个评审方意见件")
            items = [i for x in paths for i in _read_json_file(x)]
            graded = grade_review(items)
            _write_report(args.json_path, graded)
            print(f"✓ 评审分级已落盘：{len(graded['P0'])} 条 P0 / {len(graded['P1'])} 条 P1 / "
                  f"{len(graded['P2'])} 条 P2 → {args.json_path}")
            return 0

        if args.cmd == "decide-concern":
            known = {str(c.get("id")) for c in _load_jsonl(
                _meetings_dir(args.workdir) / f"concerns-{args.kind}.jsonl", strict=True)}
            if str(args.cid) not in known:      # 决议必须与关切配对：写错 id 的话这条决议什么也关不掉
                raise MeetingInputError(f"这份{MEETING_KINDS[args.kind]}关切清单里没有 {args.cid}"
                                        f"（已有：{'、'.join(sorted(known)) or '无'}）")
            rec = decide_concern(args.workdir, args.cid, args.decision, args.reason, kind=args.kind)
            _write_report(args.json_path, rec)
            print(f"✓ {MEETING_KINDS[args.kind]}关切 {rec['id']} 已决议：{rec['decision']}")
            return 0

        if args.cmd == "sync-concerns":
            paths = [x for x in str(args.inputs).split("|") if x]
            if not paths:
                raise MeetingInputError("--inputs 至少要给一个参会方关切件")
            rows = sync_reader_concerns(args.workdir, merge_concerns([_read_json_file(x) for x in paths]),
                                        kind=args.kind)
            _write_report(args.json_path, rows)      # 合并件就是这份数组（每行带稳定 id）
            print(f"✓ 关切已并条落盘：本轮 {len(rows)} 条（同句沿用原 id，重跑不追加）→ {args.json_path}")
            return 0

        if args.cmd == "reader-guidance":
            guide = reader_guidance(args.workdir, _merged_rows(args.merged), kind=args.kind)
            count = len([ln for ln in guide.splitlines() if ln.strip()])
            _write_report(args.json_path, {"kind": args.kind, "guidance": guide, "lines": count})
            if count:
                print(f"✓ 写作指导已按{MEETING_KINDS[args.kind]}决议重建：{count} 条"
                      f"（含上一轮已接受的改法）→ {args.json_path}")
            else:
                print(f"✓ 本轮没有已接受的改法（驳回项不进指导）→ {args.json_path}")
            return 0
        if args.cmd == "minutes-reader":
            decisions = _load_jsonl(_meetings_dir(args.workdir) / f"decisions-{args.kind}.jsonl", strict=True)
            minutes = build_reader_minutes(_merged_rows(args.merged), decisions)
            path = append_minutes(args.workdir, args.kind, minutes)
        else:
            minutes = build_review_minutes(_read_json_file(args.graded), args.strength, args.p0_decision)
            path = append_minutes(args.workdir, args.kind, minutes)

        landed = minutes_landed(args.workdir, args.kind)
        _write_report(args.json_path, {"kind": args.kind, "minutes": str(path), "landed": landed["landed"],
                                       "missing": landed["missing"], "fields": dict(minutes)})
        if not landed["landed"]:
            print(f"meeting_pack: 纪要没落成（{path} 缺字段：{'、'.join(landed['missing'])}）", file=sys.stderr)
            return 1
        print(f"✓ {MEETING_KINDS[args.kind]}纪要已归档：{path}（五字段齐备）")
        return 0
    except (MeetingInputError, OSError, ValueError) as err:
        print(f"meeting_pack: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
