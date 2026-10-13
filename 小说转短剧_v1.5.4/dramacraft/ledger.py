# -*- coding: utf-8 -*-
"""生成台账：append-only JSONL 事件流（v1.2.0 投产保障的地基）。

一条账三处用：预算熔断读实时调用计数、白话生产小结 production_log.md 由此派生、
词表体检的命中账同源。单进程顺序追加，断点续跑天然累加，永不改写历史。
写账失败只警告不中断主流程——台账是保险丝，不能反过来成为新的故障点。

跨进程写纪律（v1.5.0 T4 实测补强）：Windows 上 `open(path,"a")` 与 `os.open(O_APPEND)`
**都不保证原子追加**——4 进程各写 100 行实测只落 227/257 行（seek+write 非原子，互相覆盖）。
账本是伏笔核销与预算熔断的真值源，静默丢账不可接受，故 `emit` 加**真跨进程文件锁**
（Windows `msvcrt.locking`、POSIX `fcntl.flock`）；锁拿不到就按既有纪律降级为警告。
调用方仍应串行写（fbs 磁盘契约），本锁是底线而不是许可。
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:  # Windows
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None
    import fcntl


#: 等锁预算：产线实测单条追加的临界区在毫秒级，2.5 秒足够让开一整道门。
#: 1.0 秒在满负载（4 进程 × 100 行）下实测仍丢行（227/257 条），150ms 旧值更不用说——
#: 账本是伏笔核销与预算熔断的真值源，丢行等于账本不可信，宁可等也不许丢。
#: 抢锁用**有上限的等待 + 指数退避**（非阻塞尝试，轮询到预算用尽为止）：
#: 既不无限等（msvcrt.LK_LOCK 曾实测 9s+ 无上限，会把产线挂死），也不抢不到就走。
LOCK_WAIT_SECONDS = 2.5
_LOCK_POLL_MIN_SECONDS = 0.01
_LOCK_POLL_MAX_SECONDS = 0.2


class _LockedAppend:
    """跨进程互斥 + 追加写在同一句柄上。

    锁与写必须同一句柄：Windows 上同进程拿第二个带写权限的句柄会被拒（实测 EACCES）。
    锁的是第 0 个字节（所有写方锁同一处即互斥）；解锁前写完并 flush，临界区外无人能写。

    抢锁用**有上限的等待 + 指数退避**（见模块级 `LOCK_WAIT_SECONDS`）。早先只退避 3×50ms
    就放弃（打印「台账被占用」直接丢行）——满负载下 4 进程 × 100 行的压力用例实测反复丢行，
    而账本是伏笔核销与预算熔断的真值源，丢行等于账本不可信。既不无限等（LK_LOCK 曾实测 9s+
    无上限，会把产线挂死），也不抢不到就走。
    """

    def __init__(self, path: str):
        self.path = path
        self._fp = None

    def __enter__(self) -> "_LockedAppend":
        self._fp = open(self.path, "ab")      # 二进制追加：不做换行翻译，写多少字节就是多少
        try:
            self._fp.seek(0)
            if not self._acquire():
                raise OSError(f"台账被占用超过 {LOCK_WAIT_SECONDS:g} 秒")
        except OSError:
            self._fp.close()
            self._fp = None
        return self

    def _acquire(self) -> bool:
        """非阻塞抢锁 + 指数退避，等到预算用尽；拿到 True，超时 False。"""
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        delay = _LOCK_POLL_MIN_SECONDS
        while True:
            try:
                if msvcrt is not None:
                    msvcrt.locking(self._fp.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(delay)
                delay = min(delay * 2, _LOCK_POLL_MAX_SECONDS)

    @property
    def locked(self) -> bool:
        return self._fp is not None

    def append(self, payload: bytes) -> None:
        self._fp.write(payload)
        self._fp.flush()

    def __exit__(self, *exc) -> None:
        if self._fp is None:
            return
        try:
            self._fp.seek(0)
            if msvcrt is not None:
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fp.close()
            self._fp = None


class Ledger:
    def __init__(self, path: str):
        self.path = os.path.abspath(str(path))
        self._lock = threading.Lock()

    def emit(self, event: str, ep: Optional[int] = None, **fields: Any) -> None:
        record: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "event": event,
        }
        if ep is not None:
            record["ep"] = ep
        record.update(fields)
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with self._lock:
                with _LockedAppend(self.path) as handle:
                    if not handle.locked:
                        print("⚠ 生产台账正被其他任务占用，本条进度未写入（剧本正文与交付不受影响）")
                        return
                    handle.append(line)
        except OSError as e:
            print(f"⚠ 台账写入失败（不影响剧本生产）: {e}")


def load_events(path: str) -> List[Dict[str, Any]]:
    """读全部事件；文件不存在返回空列表。坏行跳过并计数（永不炸掉报告端）。"""
    events: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return events
    with open(path, "r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _transition_note(old: str, new: str, reason: str, gate_exit: str, next_step: str) -> str:
    """状态宣告四要素渲染（旧→新 / 原因 / 门禁出口 / 下一步）——照搬 fbs 状态宣告原子化。"""
    return f"旧→新：{old} → {new} ｜ 因：{reason} ｜ 门禁：{gate_exit} ｜ 下一步：{next_step}"


def _latest_run_scope(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """本次运行口径 = 最后一次 `run_start` 起的事件。

    旧实现取**最早**一次 `run_start` 推断缓存状态（`cached=N` 当成「前 N 集就绪」）。
    真跑实测（2026-09-23 closure-fix-ws）：已在库并且已导出的第 1 集被写成「⏸ 待生成」——
    那一集是在更晚的一次运行里完成的，用最早那次的状态描述当前状态必然失真。
    """
    last = None
    for i, e in enumerate(events):
        if e.get("event") == "run_start":
            last = i
    return events[last:] if last is not None else list(events)


def _latest(events: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for e in reversed(events):
        if e.get("event") == name:
            return e
    return None


def summarize(events: List[Dict[str, Any]], outline: Optional[Dict[str, Any]] = None) -> str:
    """从事件流汇总提炼白话生产小结（Markdown 表格）。
    遵守收尾铁律：严禁技术黑话（.py/.mjs/validate/gateway 等），只用导演与制片白话。

    状态一律由**本轮逐集事件**判定：`episode_generated`（引擎写的）/ `episode_cached`
    （本轮开跑前过了缓存安检、件在库里）/ `episode_undelivered`（回炉未收敛）/ `stale_cache`（旧件拒收）。
    本轮装配终验全绿（`status=passed` 且集数与规划相符）时，全部规划集都过了安检，如实标「已验收在库」。
    不做 `cached=N` 推断——只缓存第 3 集不是「前 3 集就绪」。
    """
    title = (outline.get("title") if outline else None) or "未命名短剧"

    # 1. 识别规划的所有集号
    episodes: List[int] = []
    if outline and outline.get("episodes"):
        for ep_item in outline["episodes"]:
            if "ep" in ep_item:
                episodes.append(ep_item["ep"])
    else:
        seen_eps = {e["ep"] for e in events if "ep" in e}
        episodes = sorted(list(seen_eps)) if seen_eps else [1]

    # 2. 全局状态与指标抽取
    scope = _latest_run_scope(events)
    budget_cap: Optional[int] = None
    calls_count = 0
    budget_stopped = False

    for e in events:
        evt = e.get("event")
        if evt == "run_start" and "budget_cap" in e:
            budget_cap = e.get("budget_cap")
        elif evt == "llm_call":
            calls_count += 1
        elif evt == "budget_stop":
            budget_stopped = True
            if "cap" in e and budget_cap is None:
                budget_cap = e.get("cap")
            if "calls_used" in e and calls_count == 0:
                calls_count = e.get("calls_used", 0)
        elif evt == "run_finish":
            if "calls_used" in e and calls_count == 0:
                calls_count = e.get("calls_used", 0)

    # 本轮装配是否全量通过（缺集/门禁失败都不会走到 passed，见 run_batch 的缺集防空）
    finish_ev = _latest(scope, "run_finish")
    assembled_ok = bool(finish_ev) and finish_ev.get("status") == "passed" \
        and int(finish_ev.get("episodes", -1)) == len(episodes)

    # 3. 逐集分析（只看本轮的逐集事件；来源另按全账本追）
    rows = []
    for ep_n in episodes:
        ep_scope = [e for e in scope if e.get("ep") == ep_n]
        ep_all = [e for e in events if e.get("ep") == ep_n]

        gen_ev = _latest(ep_scope, "episode_generated")
        cached_ev = _latest(ep_scope, "episode_cached")
        undelivered_ev = _latest(ep_scope, "episode_undelivered")
        stale_ev = _latest(ep_scope, "stale_cache")

        # 写作来源：引擎账本有生成记录就写记录的执笔模型；有明确外部写入事件按事件写；
        # 其余一律「来源未记录」——不拿「文件在库里」推断是谁写的。
        gen_any = _latest(ep_all, "episode_generated")
        imported_any = _latest(ep_all, "episode_imported")
        if gen_any:
            source = gen_any.get("node") or "-"
        elif imported_any:
            by = imported_any.get("by")
            source = f"外部入库（{by}）" if by else "外部入库"
        else:
            source = "来源未记录"

        retries = 0
        status = "⏸ 待生成"
        note = _transition_note("未动工", "待生成", "预算熔断暂停" if budget_stopped else "排队待产",
                                "未进闸", "充值或换节点后原命令重跑续产")

        if gen_ev:
            attempt = gen_ev.get("attempt", 1)
            retries = max(0, attempt - 1)
            status = "✅ 已过审"
            reason = f"经 {retries} 次回炉自愈后通过" if retries > 0 else "首轮直接通过"
            note = _transition_note("待生成", "已过审", reason, "12 道质量门全绿", "无需操作，已入库")
        elif cached_ev:
            status = "✅ 已验收在库"
            note = _transition_note("在库", "已验收在库", "开跑前缓存安检通过（凭据齐全、大纲哈希对得上）",
                                    "缓存安检三查通过", "无需操作，本轮已随货交付")
        elif undelivered_ev:
            status = "❌ 未交付"
            verdict = undelivered_ev.get("verdict")
            reason = ("连续两轮改动后问题没减少，需要人定稿" if verdict == "escalate"
                      else "达到最大修改轮次，本轮放弃这一集")
            note = _transition_note("生成中", "未交付", reason, "质量门未放行",
                                    "照问题摘要把稿子改掉后原命令重跑续产")
        elif any(e.get("event") in ("llm_call", "gate_fail") for e in ep_scope):
            status = "❌ 生成中断"
            note = _transition_note("生成中", "中断", "多次回炉自愈未达标", "质量门未放行",
                                    "查违规项后原命令重跑续产")
        elif stale_ev and stale_ev.get("action") == "ask":
            status = "⚠ 需处置"
            note = _transition_note("在库旧件", "需处置", str(stale_ev.get("reason") or "旧版本缓存未过安检"),
                                    "缓存安检未放行", "按终端提示选择重做或沿用后，原命令重跑")
        elif assembled_ok:
            status = "✅ 已验收在库"
            note = _transition_note("在库", "已验收在库", "本轮装配终验全绿，本集在交付清单内",
                                    "缓存安检三查通过", "无需操作，本轮已随货交付")

        rows.append({
            "ep": ep_n,
            "status": status,
            "source": source,
            "retries": retries,
            "note": note
        })

    # 4. 组装 Markdown 表格
    lines = [
        f"# 生产记录 —— 《{title}》",
        "",
        "| 集数 | 生产状态 | 写作来源 | 回炉自愈次数 | 备注 |",
        "|:---:|:---:|:---:|:---:|:---|",
    ]
    for r in rows:
        lines.append(f"| 第 {r['ep']} 集 | {r['status']} | {r['source']} | {r['retries']} | {r['note']} |")

    lines.append("")
    cap_text = f"（预算上限 {budget_cap} 次）" if budget_cap is not None else ""
    lines.append(f"模型调用：引擎账本记录的模型调用 {calls_count} 次{cap_text}；"
                 "ZCode 子代理的调用不在这个数里，本剧实际用量以运行平台的统计为准。")

    return "\n".join(lines)

