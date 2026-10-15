# -*- coding: utf-8 -*-
"""多节点 API 故障转移池。

按 llm_nodes.json 的数组顺序消耗各节点额度；额度耗尽自动顺切下一个节点。
白名单硬约束：只允许配置文件中列出的节点，不访问任何其它 API。

协议支持：
  openai    —— POST {base}/chat/completions，Bearer 鉴权，SSE 以 [DONE] 收尾
  anthropic —— POST {base}/v1/messages，x-api-key 鉴权，system 为顶层字段

失败分类：
  quota              → 永久跳过该节点，顺切下一个
  degrade            → 去掉 stream_options 后重试同一节点
  rate_limit         → 同节点退避重试
  transient          → 同节点退避重试
  client_input_error → 输入错误直接抛给调用方，不禁用节点
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests

VALID_PROTO = ("openai", "anthropic")
DEFAULT_MAX_TOKENS = 28000
ANTHROPIC_VERSION = "2023-06-01"

# 模板占位符特征（SKILL.md 0.2 承诺：仍为占位符时建库前置 BLOCKED）。
# 只认明确是模板残留的片段，避免误伤真实密钥。
_PLACEHOLDER_MARKERS = (
    "REPLACE", "PLACEHOLDER", "YOUR-", "YOUR_", "CHANGE_ME", "CHANGE-ME",
    "CHANGEME", "占位", "请填写", "你的密钥", "SK-YOUR",
)


def looks_like_placeholder_key(key: str) -> bool:
    """判断 api_key 是否仍是模板占位符（空值也算未配置）。"""
    k = (key or "").strip()
    if not k:
        return True
    up = k.upper()
    return any(m in up for m in _PLACEHOLDER_MARKERS)


class QuotaExhaustedError(RuntimeError):
    """节点额度耗尽或永久不可用——应永久跳过该节点。"""


class TransientError(RuntimeError):
    """瞬时错误——应在同一节点退避重试。"""


class RateLimitError(RuntimeError):
    """限流（429 无额度关键词）——额度仍可能充足，应长退避重试而非永久弃用。

    与 QuotaExhaustedError 的区别：限流是暂时性的，节点额度未必用尽；
    若直接永久跳过，会浪费该节点剩余额度。
    """


class ClientInputError(RuntimeError):
    """客户端输入错误（HTTP 400 业务错误，如文本超长、Bad Request）。

    坏的是输入不是节点：直接抛给调用方（由上层标记该段失败/跳过），
    绝不禁用节点、不顺切——否则一个超长段就会击穿整个节点池（KB-02）。
    """


class AllNodesExhaustedError(RuntimeError):
    """全部节点不可用——任务必须停止。"""


@dataclass
class Node:
    """一个 API 节点。"""

    name: str
    proto: str
    base_url: str
    model: str
    api_key: str
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_concurrency: int = 0
    no_proxy: bool = False
    # 运行时状态
    exhausted: bool = False
    exhausted_reason: str = ""
    served: int = 0
    _sem: threading.Semaphore | None = field(default=None, repr=False, compare=False)

    @property
    def is_healthy(self) -> bool:
        """节点是否健康（未被标记失效）；输入错误不影响健康状态。"""
        return not self.exhausted

    def endpoint(self) -> str:
        """完整请求地址。"""
        base = self.base_url.rstrip("/")
        if self.proto == "anthropic":
            if base.endswith("/v1"):
                return base + "/messages"
            return base + "/v1/messages"
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def headers(self) -> dict[str, str]:
        """鉴权与内容类型头（按协议区分）。"""
        if self.proto == "anthropic":
            return {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            }
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def payload(self, system_prompt: str, user_prompt: str,
                max_tokens: int) -> dict[str, Any]:
        """请求体（按协议区分；anthropic 的 system 是顶层字段）。"""
        requested = int(max_tokens)
        eff = min(requested, int(self.max_tokens)) if self.max_tokens else requested
        if self.proto == "anthropic":
            return {
                "model": self.model,
                "max_tokens": eff,
                "temperature": 0.4,
                "stream": True,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }
        return {
            "model": self.model,
            "max_tokens": eff,
            "temperature": 0.4,
            "stream": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

    def semaphore(self) -> threading.Semaphore:
        """节点级并发闸门（max_concurrency=0 表示不限）。"""
        if self._sem is None:
            limit = self.max_concurrency if self.max_concurrency > 0 else 4096
            self._sem = threading.Semaphore(limit)
        return self._sem

    def proxies(self) -> dict[str, str] | None:
        """请求代理配置。no_proxy=True 时强制直连（绕过全局 ALL_PROXY）。

        部分厂商（如 SenseAudio）的 TLS 终结点拒绝经代理握手，必须直连。
        """
        if self.no_proxy:
            return {"http": "", "https": ""}
        return None


class SSEResult:
    """流式响应聚合结果。"""

    __slots__ = ("parts", "usage", "truncated", "done")

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.usage: dict[str, Any] = {}
        self.truncated = False
        self.done = False

    def content(self) -> str:
        return "".join(self.parts).strip()


def parse_openai_chunk(chunk: dict[str, Any], result: SSEResult) -> None:
    """解析 OpenAI 兼容 SSE 帧。"""
    for ch in chunk.get("choices") or []:
        fr = ch.get("finish_reason")
        if fr:
            if fr == "length":
                result.truncated = True
            result.done = True
        delta = ch.get("delta") or {}
        if delta.get("content"):
            result.parts.append(delta["content"])
    if chunk.get("usage"):
        result.usage = chunk["usage"]


def parse_anthropic_event(event: dict[str, Any], result: SSEResult) -> None:
    """解析 Anthropic Messages SSE 事件（thinking_delta 不计入正文）。"""
    etype = event.get("type")
    if etype == "message_start":
        usage = (event.get("message") or {}).get("usage") or {}
        if usage:
            result.usage["prompt_tokens"] = usage.get("input_tokens", 0)
    elif etype == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta" and delta.get("text"):
            result.parts.append(delta["text"])
    elif etype == "message_delta":
        delta = event.get("delta") or {}
        if delta.get("stop_reason") == "max_tokens":
            result.truncated = True
        usage = event.get("usage") or {}
        if usage:
            result.usage["completion_tokens"] = usage.get("output_tokens", 0)
    elif etype == "message_stop":
        result.done = True


_THINK_FENCE_RE = re.compile(
    r"^\s*```(?:think|thinking|reasoning)\b[^\n]*\n.*?```\s*", re.S | re.I
)
_THINK_TAG_RE = re.compile(
    r"^\s*<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>\s*", re.S | re.I
)


def strip_reasoning(text: str) -> str:
    """剥离模型思考块（部分模型默认开思考会污染正文）。

    只处理**开头**的思考块，正文中间出现的 'think' 单词不受影响。
    """
    t = text or ""
    prev = None
    while prev != t:
        prev = t
        t = _THINK_FENCE_RE.sub("", t, count=1)
        t = _THINK_TAG_RE.sub("", t, count=1)
    return t.lstrip("\n")


_QUOTA_KEYWORDS = (
    "insufficient_quota", "insufficient quota", "quota exceeded", "quota_exhausted",
    "quota exhausted", "quota_exceeded", "exceeded quota", "out of quota",
    "余额不足", "额度不足", "额度已用尽", "额度耗尽", "配额已用尽", "欠费", "billing",
    "exceeded your current quota",
    "model must be one of", "model not found", "does not exist", "invalid model",
    "no such model", "unsupported model",
)
_DEGRADE_KEYWORDS = ("stream_options", "include_usage", "unrecognized request argument")


def classify_http_failure(status: int, body: str) -> str:
    """把 HTTP 失败归类为 quota / degrade / rate_limit / transient /
    client_input_error。

    quota              → 永久跳过该节点，顺切下一个
    degrade            → 去掉 stream_options 后重试同一节点
    rate_limit         → 同节点退避重试
    transient          → 同节点退避重试
    client_input_error → 输入错误直接抛给调用方，不禁用节点
    """
    low = (body or "").lower()
    if status == 429:
        if any(k in low for k in _QUOTA_KEYWORDS):
            return "quota"
        return "rate_limit"
    if status in (401, 402, 403):
        return "quota"
    if status == 400:
        if any(k in low for k in _DEGRADE_KEYWORDS):
            return "degrade"
        # 业务输入错误（超长、Bad Request）：坏的是输入，严禁禁用节点。
        return "client_input_error"
    if status == 404:
        return "quota"
    if status >= 500:
        return "transient"
    return "transient"


def _parse_node(raw: dict[str, Any], idx: int) -> Node:
    name = str(raw.get("name") or f"node-{idx}").strip()
    proto = str(raw.get("proto") or "").strip().lower()
    if proto not in VALID_PROTO:
        raise ValueError(
            f"节点 {name} 的 proto 非法: {proto!r}（仅支持 {VALID_PROTO}）"
        )
    base_url = str(raw.get("base_url") or "").strip()
    model = str(raw.get("model") or "").strip()
    api_key = str(raw.get("api_key") or "").strip()
    for fld, val in (("base_url", base_url), ("model", model), ("api_key", api_key)):
        if not val:
            raise ValueError(f"节点 {name} 缺少必填字段 {fld}")
    return Node(
        name=name,
        proto=proto,
        base_url=base_url,
        model=model,
        api_key=api_key,
        max_tokens=int(raw.get("max_tokens") or DEFAULT_MAX_TOKENS),
        max_concurrency=int(raw.get("max_concurrency") or 0),
        no_proxy=bool(raw.get("no_proxy", False)),
    )


class ApiPool:
    """按顺序消耗多节点额度的调用池（线程安全）。"""

    # 瞬时错误在同节点重试次数与退避基数（秒）
    transient_retries = 2
    transient_backoff = 5.0
    # 限流（429）在同节点重试次数与退避基数（秒）——比瞬时错误更密更长
    rate_limit_retries = 3
    rate_limit_backoff = 15.0
    # 事件日志路径（由 build_segment_index 注入；None 则不落盘）
    event_log_path: str | None = None

    def _log_event(self, kind: str, node: Node, detail: str = "") -> None:
        """把节点事件追加写入日志文件（stdout 被缓冲时仍可审计顺切行为）。"""
        path = self.event_log_path
        if not path:
            return
        try:
            line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{kind}\t"
                    f"{node.name}\t{detail}\n")
            with self._lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            pass

    def revive_rate_limited(self) -> list[str]:
        """把因限流/瞬时错误被弃用的节点重新纳入候选（额度未必用尽）。

        只复活「非额度原因」失效的节点；真正额度耗尽（QuotaExhausted）的
        节点保持失效，避免反复撞墙。每次调用前执行，保证限流窗口过后
        该节点能重新贡献额度。
        """
        revived: list[str] = []
        with self._lock:
            for n in self.nodes:
                if n.exhausted and not n.exhausted_reason.startswith("额度"):
                    n.exhausted = False
                    n.exhausted_reason = ""
                    revived.append(n.name)
            if revived:
                self._idx = 0  # 从头重扫，优先回到高顺位节点
        return revived

    def __init__(self, nodes: list[Node]):
        if not nodes:
            raise ValueError("节点列表不能为空：至少需要一个 LLM 节点")
        self.nodes = nodes
        self._lock = threading.Lock()
        self._idx = 0

    @classmethod
    def from_config(cls, path: str) -> "ApiPool":
        if not os.path.isfile(path):
            raise FileNotFoundError(f"节点配置文件不存在: {path}")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        raw_nodes = data.get("nodes") if isinstance(data, dict) else None
        if not raw_nodes:
            raise ValueError("节点配置为空：llm_nodes.json 的 nodes 数组至少需要 1 个节点")
        return cls([_parse_node(r, i) for i, r in enumerate(raw_nodes)])

    def current(self) -> Node | None:
        """当前活动节点；已失效的自动跳过。全部失效返回 None。"""
        with self._lock:
            while self._idx < len(self.nodes):
                node = self.nodes[self._idx]
                if not node.exhausted:
                    return node
                self._idx += 1
            return None

    def mark_exhausted(self, node: Node, reason: str) -> None:
        with self._lock:
            node.exhausted = True
            node.exhausted_reason = reason

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for n in self.nodes if not n.exhausted)

    def placeholder_nodes(self) -> list[str]:
        """列出密钥仍为占位符的节点名（只给名字，绝不回显密钥内容）。"""
        with self._lock:
            return [n.name for n in self.nodes
                    if looks_like_placeholder_key(n.api_key)]

    def summary(self) -> str:
        """节点状态摘要（只含节点名与段数，绝不回显密钥）。"""
        with self._lock:
            parts = []
            for n in self.nodes:
                if n.exhausted:
                    parts.append(f"{n.name}[失效:{n.exhausted_reason[:40]}]")
                else:
                    parts.append(f"{n.name}[{n.served}段]")
            return " → ".join(parts)

    def call(self, system_prompt: str, user_prompt: str,
             max_tokens: int = DEFAULT_MAX_TOKENS) -> tuple[str, dict, str]:
        """依次尝试可用节点；额度耗尽自动顺切。

        返回 (正文, usage, 实际服务节点名)。
        输入错误（ClientInputError）直接抛出，不顺切、不禁用节点。
        """
        reasons: list[str] = []
        while True:
            self.revive_rate_limited()
            node = self.current()
            if node is None:
                detail = "；".join(reasons) if reasons else "无可用节点"
                raise AllNodesExhaustedError(
                    f"全部 {len(self.nodes)} 个 API 节点均已耗尽/不可用：{detail}"
                )
            attempt = 0
            while True:
                try:
                    with node.semaphore():
                        content, usage = self._call_node(
                            node, system_prompt, user_prompt, max_tokens
                        )
                    with self._lock:
                        node.served += 1
                    self._log_event("SERVED", node, "段调用成功")
                    return content, usage, node.name
                except QuotaExhaustedError as exc:
                    reasons.append(f"{node.name}: {exc}")
                    self.mark_exhausted(node, f"额度耗尽: {exc}")
                    self._log_event("EXHAUSTED", node, str(exc)[:200])
                    break  # 顺切下一个节点
                except RateLimitError as exc:
                    if attempt >= self.rate_limit_retries:
                        # 长退避后仍限流：临时跳过本节点（可被 revive 复活），顺切下一个
                        reasons.append(f"{node.name}: 限流退避耗尽({exc})")
                        self.mark_exhausted(node, f"限流退避耗尽: {exc}")
                        self._log_event("RATE_LIMITED", node, str(exc)[:200])
                        break
                    attempt += 1
                    wait = self.rate_limit_backoff * attempt
                    self._log_event("RATE_WAIT", node,
                                    f"限流第{attempt}次退避 {wait:.0f}s")
                    time.sleep(wait)
                except TransientError as exc:
                    if attempt >= self.transient_retries:
                        reasons.append(f"{node.name}: 瞬时错误重试耗尽({exc})")
                        self.mark_exhausted(node, f"瞬时错误重试耗尽: {exc}")
                        self._log_event("EXHAUSTED", node, f"瞬时错误重试耗尽: {exc}"[:200])
                        break
                    attempt += 1
                    self._log_event("RETRY", node, f"瞬时错误第{attempt}次重试: {exc}"[:200])
                    time.sleep(self.transient_backoff * attempt)

    def _call_node(self, node: Node, system_prompt: str, user_prompt: str,
                   max_tokens: int) -> tuple[str, dict]:
        """单节点流式调用（协议自适应）。

        失败抛 QuotaExhaustedError / RateLimitError / TransientError /
        ClientInputError；其中输入错误不重试、不禁用节点，直接上抛。
        """
        include_usage = True
        payload = node.payload(system_prompt, user_prompt, max_tokens)
        last_transient = ""
        for attempt in range(2):
            if include_usage and node.proto == "openai":
                payload["stream_options"] = {"include_usage": True}
            try:
                resp = requests.post(
                    node.endpoint(), json=payload, headers=node.headers(),
                    timeout=(30, 600), stream=True, proxies=node.proxies(),
                )
            except requests.RequestException as exc:
                last_transient = f"连接失败: {exc}"
                time.sleep(5)
                continue
            if resp.status_code != 200:
                body = ""
                try:
                    body = resp.text[:600]
                except Exception:
                    pass
                finally:
                    resp.close()
                kind = classify_http_failure(resp.status_code, body)
                if kind == "degrade":
                    include_usage = False
                    payload.pop("stream_options", None)
                    continue
                msg = f"HTTP {resp.status_code} {body[:200]}"
                if kind == "client_input_error":
                    raise ClientInputError(msg)
                if kind == "quota":
                    raise QuotaExhaustedError(msg)
                if kind == "rate_limit":
                    raise RateLimitError(msg)
                last_transient = msg
                time.sleep(5 * (attempt + 1))
                continue
            result = self._read_stream(node, resp)
            if result.truncated:
                raise TransientError("输出被 max_tokens 截断")
            content = strip_reasoning(result.content())
            if not content:
                last_transient = "流式响应为空"
                continue
            return content, result.usage
        raise TransientError(last_transient or "未知失败")

    @staticmethod
    def _read_stream(node: Node, resp) -> SSEResult:
        """按字节缓冲分割 SSE 行（防 UTF-8 中文 0x85 字节被误判为换行）。"""
        result = SSEResult()
        buf = bytearray()
        try:
            for raw in resp.iter_content(8192):
                if result.done:
                    break
                buf += raw
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    sline = line.strip()
                    if not sline.startswith(b"data:"):
                        continue
                    data = sline[5:].strip()
                    if data == b"[DONE]":
                        result.done = True
                        buf = bytearray()
                        break
                    try:
                        obj = json.loads(data.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if node.proto == "anthropic":
                        parse_anthropic_event(obj, result)
                    else:
                        parse_openai_chunk(obj, result)
        finally:
            resp.close()
        return result
