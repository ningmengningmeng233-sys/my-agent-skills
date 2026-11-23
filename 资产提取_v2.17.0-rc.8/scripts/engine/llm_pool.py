# -*- coding: utf-8 -*-
"""多节点调用池：按数组顺序消耗、每节点并发上限、失败四分类（设计方案 §3.1/§4）。"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional

import requests

DEFAULT_MAX_TOKENS = 8000
DEFAULT_MAX_CONCURRENCY = 4
CONFIG_NAME = "llm_nodes.json"
RATE_LIMIT_COOLDOWN_SECONDS = 30
TRANSIENT_RETRIES = 3


class BudgetExceeded(RuntimeError):
    """预算超限：立即停止，消息含已用次数与上限。"""

    code = "BUDGET_EXCEEDED"

    def __init__(self, used: int, limit: int):
        super().__init__(f"预算超限：已用 {used} / 上限 {limit}")
        self.used = used
        self.limit = limit


class AllNodesExhausted(RuntimeError):
    """全池耗尽：稳定原因码，任务不静默丢失。"""

    code = "ALL_NODES_EXHAUSTED"

    def __init__(self, detail: str = ""):
        super().__init__(detail or self.code)
        self.detail = detail or self.code


class _NodeFailure(RuntimeError):
    """节点级失败信号：kind ∈ quota | rate_limit | transient | degrade | fatal。"""

    def __init__(self, kind: str, detail: str = "", wait: float = 0.0):
        super().__init__(detail or kind)
        self.kind = kind
        self.detail = detail
        self.wait = wait


def classify_failure(status: int, body: str) -> str:
    """失败四分类（设计方案 §4）：quota | rate_limit | transient | degrade | fatal。"""
    text = (body or "").lower()
    if "stream_options" in text or "include_usage" in text or "unrecognized request argument" in text:
        return "degrade"
    if "quota" in text:
        return "quota"
    if status == 429 or "rate limit" in text:
        return "rate_limit"
    if status >= 500 or "timeout" in text or "connection" in text:
        return "transient"
    return "fatal"


@dataclass(frozen=True)
class Node:
    name: str
    proto: str
    base_url: str
    model: str
    api_key: str = ""
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    no_proxy: bool = False
    roles: tuple[str, ...] = ()      # 空 = 通吃（T13 分档模型）

    def __repr__(self) -> str:            # 密钥不进日志
        return f"Node(name={self.name!r}, proto={self.proto!r}, model={self.model!r})"


def _candidates(env: Mapping[str, str], search: Optional[Iterable[Path]] = None) -> List[Path]:
    out: List[Path] = []
    env = env or {}
    p = env.get("ASSET_ENV_PATH")
    if p:
        out.append(Path(p).expanduser().parent / CONFIG_NAME)
    skill_dir = Path(__file__).resolve().parent.parent.parent
    out.append(skill_dir / CONFIG_NAME)
    home_cfg = Path.home() / ".asset-extract" / CONFIG_NAME
    from engine.offline import offline_requested

    # 离线模式屏蔽隐式家目录池配置（B8 同类回退：池节点同样能指向计费端点）。
    if not offline_requested(env):
        out.append(home_cfg)
    if search:
        out.extend(Path(s) / CONFIG_NAME for s in search)
    return out


def load_nodes(env: Optional[Mapping[str, str]] = None,
               search: Optional[Iterable[Path]] = None) -> List[Node]:
    source = dict(os.environ if env is None else env)
    for path in _candidates(source, search):
        if not path.is_file():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        nodes: List[Node] = []
        for item in raw.get("nodes") or []:
            nodes.append(Node(
                name=str(item.get("name") or f"node{len(nodes)+1}"),
                proto=str(item.get("proto") or "openai"),
                base_url=str(item.get("base_url") or "").rstrip("/"),
                model=str(item.get("model") or ""),
                api_key=str(item.get("api_key") or ""),
                max_tokens=int(item.get("max_tokens") or DEFAULT_MAX_TOKENS),
                max_concurrency=int(item.get("max_concurrency") or DEFAULT_MAX_CONCURRENCY),
                no_proxy=bool(item.get("no_proxy") or False),
                roles=tuple(str(r) for r in (item.get("roles") or [])),
            ))
        return [n for n in nodes if n.base_url and n.model]
    return []


def single_node_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[Node]:
    """无池配置时的回落：把现有三件套环境变量包成一个节点。"""
    source = dict(os.environ if env is None else env)
    url = str(source.get("SUMMARY_API_URL") or "").strip()
    key = str(source.get("OPENAI_API_KEY") or "").strip()
    model = str(source.get("SUMMARY_API_MODEL") or "").strip()
    if not url or not model:
        return None
    return Node(name="env-single", proto="openai", base_url=url.rsplit("/chat/completions", 1)[0],
                model=model, api_key=key,
                max_tokens=int(source.get("SUMMARY_MAX_TOKENS") or DEFAULT_MAX_TOKENS))


EVENTS_NAME = "调用事件.jsonl"
CACHE_DIRNAME = "调用缓存"


def from_env(lib=None, force: bool = False, search: Optional[Iterable[Path]] = None,
             env: Optional[Mapping[str, str]] = None) -> CallPool:
    """从环境构造调用池（工单 T7）：池文件优先，回落单端点三件套。"""
    source = dict(os.environ if env is None else env)
    nodes = load_nodes(env=source, search=search)
    if not nodes:
        one = single_node_from_env(source)
        if one is None:
            raise RuntimeError("未配置任何调用通道（llm_nodes.json 或 SUMMARY_API_URL 三件套）")
        nodes = [one]
    workers = int(os.environ.get("ASSET_WORKERS") or DEFAULT_MAX_CONCURRENCY * 2)
    max_calls = os.environ.get("ASSET_MAX_CALLS")
    max_seconds = os.environ.get("ASSET_MAX_SECONDS")
    return CallPool(nodes, global_limit=workers, lib=lib,
                    max_calls=int(max_calls) if max_calls else None,
                    max_seconds=float(max_seconds) if max_seconds else None,
                    force=force)


def cache_path(lib, cache_key: str) -> Path:
    """cache_key 的 sha256 → <lib>/.process/调用缓存/<hash>.json。"""
    import hashlib
    digest = hashlib.sha256(str(cache_key).encode("utf-8")).hexdigest()
    return Path(lib) / ".process" / CACHE_DIRNAME / f"{digest}.json"


def append_event(lib, event: str, node: str = "", detail: str = "") -> None:
    """追加一行调用事件到 <lib>/.process/调用事件.jsonl（写前日志风格同 visual_events）。

    detail 只放原因码与非敏感信息（不含 prompt 正文、不含密钥）。
    """
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": str(event),
           "node": str(node), "detail": str(detail or "")}
    path = Path(lib) / ".process" / EVENTS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class CallPool:
    """按优先级选节点；节点闸门与全局上限取小；失败按四分类接力。"""

    def __init__(self, nodes: List[Node], global_limit: int = DEFAULT_MAX_CONCURRENCY * 2,
                 lib: Optional[Path] = None, max_calls: Optional[int] = None,
                 max_seconds: Optional[float] = None, force: bool = False):
        self._nodes = list(nodes)
        self._global = threading.Semaphore(max(1, int(global_limit)))
        self._gates = {n.name: threading.Semaphore(max(1, n.max_concurrency)) for n in self._nodes}
        self._skipped = set()
        self._cooldowns = {}        # 节点名 -> 冷却截止时间戳
        self._no_stream_options = set()   # 已知不支持 include_usage 的节点
        self._lib = Path(lib) if lib is not None else None
        self._max_calls = int(max_calls) if max_calls else None
        self._max_seconds = float(max_seconds) if max_seconds else None
        self._used_calls = 0
        self._budget_lock = threading.Lock()
        self._started_at = time.time()
        self._force = bool(force)

    @property
    def force(self) -> bool:
        return self._force

    @force.setter
    def force(self, value: bool) -> None:
        self._force = bool(value)

    def skipped_nodes(self) -> List[str]:
        """被永久跳过的节点名（按配置顺序）。"""
        return [n.name for n in self._nodes if n.name in self._skipped]

    def _event(self, event: str, node: str, detail: str = "") -> None:
        """调用事件日志（写前追加，零密钥/零正文）；无 lib 时静默跳过。"""
        if self._lib is None:
            return
        append_event(self._lib, event, node, detail)

    def _pick(self, role: str = "judgement") -> Optional[Node]:
        now = time.time()
        available = []
        for node in self._nodes:
            if node.name in self._skipped:
                continue
            cd = self._cooldowns.get(node.name)
            if cd is not None and now < cd:
                continue
            available.append(node)
        # T13：优先选声明了该 role 的节点；没有则回落到全部可用节点（roles 空 = 通吃）
        for node in available:
            if role in node.roles:
                return node
        return available[0] if available else None

    def _reserve_budget(self) -> bool:
        """原子预留一次调用预算；无预算配置返回 False；超限抛 BudgetExceeded。"""
        if self._max_calls is None:
            return False
        with self._budget_lock:
            if self._used_calls >= self._max_calls:
                used = self._used_calls
            else:
                self._used_calls += 1
                return True
        self._event("budget", "-", f"已用 {used} / 上限 {self._max_calls}")
        raise BudgetExceeded(used, self._max_calls)

    def _refund_budget(self) -> None:
        """退还一次预留（分发失败时调用，与 _reserve_budget 配对，失败不耗预算）。"""
        if self._max_calls is None:
            return
        with self._budget_lock:
            self._used_calls -= 1

    def call(self, system_prompt: str, user_prompt: str, **kw):
        cache_key = kw.pop("cache_key", None)
        kw.pop("step", None)          # step 由账本层使用；池内不再向下传
        role = str(kw.pop("role", None) or "judgement")   # 缺省=判断活走强模型（T13）
        last_detail = ""
        while True:
            if cache_key and not self._force:
                hit = self._cache_read(cache_key)
                if hit is not None:
                    self._event("skipped", "-", str(cache_key)[:80])
                    return hit
            if cache_key and self._force:
                self._event("force", "-", str(cache_key)[:80])
                cache_key = None      # 只对本次调用绕过缓存
            if self._max_calls is not None and self._used_calls >= self._max_calls:
                self._event("budget", "-", f"已用 {self._used_calls} / 上限 {self._max_calls}")
                raise BudgetExceeded(self._used_calls, self._max_calls)
            if self._max_seconds is not None and time.time() - self._started_at >= self._max_seconds:
                self._event("budget", "-", f"耗时超上限 {self._max_seconds}s")
                raise BudgetExceeded(self._used_calls, int(self._max_seconds))
            node = self._pick(role)
            if node is None:
                if all(n.name in self._skipped for n in self._nodes):
                    self._event("exhausted", "-", last_detail)
                    raise AllNodesExhausted(last_detail)
                time.sleep(0.02)      # 有节点只是冷却中，稍候重选
                continue
            reserved = self._reserve_budget()
            self._event("pick", node.name)
            self.last_node = node.name
            try:
                with self._global, self._gates[node.name]:
                    result = self._dispatch(node, system_prompt, user_prompt, **kw)
            except _NodeFailure as e:
                if reserved:
                    self._refund_budget()
                last_detail = f"{node.name}: {e.kind} {e.detail}"[:200]
                self._event(e.kind, node.name, e.detail[:120])
                if e.kind == "quota":
                    self._skipped.add(node.name)      # 额度耗尽 → 本轮不再选中
                elif e.kind == "rate_limit":
                    self._cooldowns[node.name] = time.time() + RATE_LIMIT_COOLDOWN_SECONDS
                else:
                    # transient 重试耗尽 / degrade 无法降级 / fatal：淘汰该节点
                    self._skipped.add(node.name)
                continue  # 节点级失败已记账回滚：换节点重试，不走成功路径
            except BaseException:
                if reserved:
                    self._refund_budget()
                raise
            self._event("ok", node.name)
            if cache_key:
                self._cache_write(cache_key, result)
            return result

    def _cache_read(self, cache_key: str):
        """命中即返回 (正文, usage[cache_hit=True])；未命中返回 None。"""
        path = cache_path(self._lib, cache_key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        usage = dict(data.get("usage") or {})
        usage["cache_hit"] = True
        return data.get("text", ""), usage

    def _cache_write(self, cache_key: str, result) -> None:
        """成功结果落缓存（正文 + 非敏感 usage 数字）。"""
        if self._lib is None:
            return
        text, usage = result
        keep = {k: usage.get(k) for k in ("model", "prompt_tokens", "completion_tokens",
                                          "input_chars", "output_chars") if k in usage}
        path = cache_path(self._lib, cache_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"text": text, "usage": keep}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _dispatch(self, node: Node, system_prompt: str, user_prompt: str, **kw):
        from engine import llm as llm_client

        extra_req = kw.pop("extra_requirements", None)
        if extra_req:
            system_prompt = system_prompt + "\n" + "\n".join(str(x) for x in extra_req)
        max_tokens = kw.get("max_tokens") or node.max_tokens
        temperature = kw.get("temperature", 0.4)
        min_len = kw.get("min_len", 0)
        payload: dict = {
            "model": node.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if node.name in self._no_stream_options:
            payload.pop("stream_options", None)
        if llm_client.thinking_disabled():
            payload["thinking"] = {"type": "disabled"}
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {node.api_key}"}
        from engine.offline import ensure_online

        # 传输层守门（B8）：离线模式在任何 socket 之前抛 OfflineRequested；
        # 单测假端点走显式 loopback，予以放行。
        ensure_online(f"节点池调用（{node.name}）", url=f"{node.base_url}/chat/completions")
        started = time.time()
        attempt = 0
        while True:
            attempt += 1
            resp = requests.post(f"{node.base_url}/chat/completions", json=payload,
                                 headers=headers, timeout=(30, 600), stream=True)
            if resp.status_code != 200:
                try:
                    body = resp.text
                except Exception:
                    body = ""
                finally:
                    resp.close()
                kind = classify_failure(resp.status_code, body)
                if kind == "transient" and attempt <= TRANSIENT_RETRIES:
                    time.sleep(min(60, 10 * (2 ** (attempt - 1))))
                    continue
                if kind == "degrade" and "stream_options" in payload:
                    payload.pop("stream_options", None)
                    continue
                raise _NodeFailure(kind, f"HTTP {resp.status_code} {body[:120]}")
            parts, usage, finish_reason = llm_client.consume_sse(resp)
            content = "".join(parts).strip()
            if not content:
                if attempt > TRANSIENT_RETRIES:
                    raise _NodeFailure("transient", "流式响应为空")
                continue
            if min_len and len(content) < min_len:
                if attempt > TRANSIENT_RETRIES:
                    raise _NodeFailure("transient", f"输出过短（{len(content)} < {min_len}）")
                continue
            if finish_reason == "length":
                raise _NodeFailure("fatal", f"输出截断（finish_reason=length，{len(content)} 字符）")
            if finish_reason not in (None, "stop"):
                if attempt > TRANSIENT_RETRIES:
                    raise _NodeFailure("transient", f"异常结束（finish_reason={finish_reason}）")
                continue
            from engine.work_scheduler import normalize_usage

            raw_usage = dict(usage or {})
            input_chars = len(system_prompt) + len(user_prompt)
            normalized = normalize_usage(raw_usage, input_chars=input_chars, landed_chars=len(content))
            normalized.update(raw_usage)
            normalized["cache_hit"] = bool(raw_usage.get("cache_hit", False))
            normalized["input_chars"] = input_chars
            normalized["output_chars"] = len(content)
            normalized["elapsed_ms"] = int((time.time() - started) * 1000)
            normalized["attempts"] = attempt
            normalized["model"] = node.model
            return content, normalized
