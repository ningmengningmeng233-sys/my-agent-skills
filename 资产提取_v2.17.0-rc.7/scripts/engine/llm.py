#!/usr/bin/env python3
"""Streaming LLM client with 429 backoff and usage tracking."""
import json
import os
import sys
import time
from pathlib import Path
from threading import Lock
from typing import Any

import requests

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent.parent

try:
    from engine.common import load_env

    load_env()
except Exception:
    pass

from engine.work_scheduler import DEFAULT_CAPS, QUEUE_EXTRACT, normalize_usage  # noqa: E402

API_URL = os.environ.get("SUMMARY_API_URL", "https://api.openai.com/v1/chat/completions")
MAX_TOKENS = int(os.environ.get("SUMMARY_MAX_TOKENS", "8000"))
API_KEY = os.environ.get("OPENAI_API_KEY", "")
API_MODEL = os.environ.get("SUMMARY_API_MODEL", "gpt-4o-mini")
# 并发默认值取调度器给「提取」队列的能力上限，不再把固定 32 当正确答案（工单 9）。
WORKERS = int(os.environ.get("ASSET_WORKERS", str(DEFAULT_CAPS[QUEUE_EXTRACT])))


class LLMAuthError(RuntimeError):
    """Authentication failure; retrying is pointless."""


def thinking_disabled() -> bool:
    """是否要求端点关闭推理（环境变量 `ASSET_THINKING_DISABLED`）。

    推理模型会先把 max_tokens 预算花在 reasoning_content 上，正文因预算耗尽返回空串
    且 finish_reason=length——链路里表现为「流式响应为空」与「输出截断」。
    实测 `{"type": "disabled"}` 对 DeepSeek 系生效（推理 token 归零、正文正常返回）；
    `enable_thinking: false` 无效。

    **默认关闭**：不接受该字段的端点，请求体与历史版本逐字节一致；本机反代不吃这个开关
    （实测六种写法推理 token 均不降），开了也无害。按值读取而非导入期读取，便于测试。
    """
    value = os.environ.get("ASSET_THINKING_DISABLED", "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _nodes():
    """节点池候选列表（工单 T1）：有池配置按文件顺序，无池回落单端点三件套。"""
    from engine import llm_pool
    nodes = llm_pool.load_nodes()
    if nodes:
        return nodes
    one = llm_pool.single_node_from_env()
    return [one] if one else []


_POOL_LOCK = Lock()
_pool_cache = None


def _get_pool():
    """有 llm_nodes.json 时返回共享 CallPool；无池配置返回 None（走原路径）。"""
    global _pool_cache
    from engine import llm_pool
    if _pool_cache is None:
        with _POOL_LOCK:
            if _pool_cache is None:
                nodes = llm_pool.load_nodes()
                if nodes:
                    max_calls = os.environ.get("ASSET_MAX_CALLS")
                    max_seconds = os.environ.get("ASSET_MAX_SECONDS")
                    _pool_cache = llm_pool.CallPool(
                        nodes, global_limit=WORKERS,
                        max_calls=int(max_calls) if max_calls else None,
                        max_seconds=float(max_seconds) if max_seconds else None)
                else:
                    _pool_cache = False
    return _pool_cache or None


def consume_sse(resp) -> tuple[list[str], dict[str, Any], Any]:
    """消费 OpenAI SSE 流：返回 (内容分片, usage, finish_reason)。"""
    parts, usage, finish_reason = [], {}, None
    done = False
    buf = bytearray()
    for raw in resp.iter_content(8192):
        if done:
            break
        buf += raw
        while b"\n" in buf:
            line, _, buf = buf.partition(b"\n")
            sline = line.strip()
            if not sline.startswith(b"data:"):
                continue
            data = sline[5:].strip()
            if data == b"[DONE]":
                buf = bytearray()
                done = True
                break
            try:
                chunk = json.loads(data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            for ch in chunk.get("choices", []):
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    parts.append(delta["content"])
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
            if chunk.get("usage"):
                usage = chunk["usage"]
    return parts, usage, finish_reason


_print_lock = Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def call_llm(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int | None = None,
    retries: int = 3,
    temperature: float = 0.4,
    min_len: int = 0,
    step: str = "未命名",
    extra_requirements: tuple[str, ...] | None = None,
    role: str = "judgement",
) -> tuple[str, dict[str, Any]]:
    """Stream-call the LLM. Return (content, usage)."""
    if extra_requirements:
        system_prompt = system_prompt + "\n" + "\n".join(str(x) for x in extra_requirements)
    started = time.time()
    pool = _get_pool()
    try:
        if pool is not None:
            content, usage = pool.call(system_prompt, user_prompt, max_tokens=max_tokens,
                                       retries=retries, temperature=temperature, min_len=min_len,
                                       role=role)
        else:
            content, usage = _call_single(system_prompt, user_prompt, max_tokens=max_tokens,
                                          retries=retries, temperature=temperature,
                                          min_len=min_len)
    except Exception as e:
        _ledger_write(step, API_MODEL, _pool_node_name(pool), int((time.time() - started) * 1000),
                      retries=retries, error=str(e)[:160], role=role)
        raise
    _ledger_write(step, usage.get("model") or API_MODEL, _pool_node_name(pool),
                  int(usage.get("elapsed_ms") or (time.time() - started) * 1000),
                  prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                  retries=int(usage.get("attempts") or 1) - 1, error="", role=role)
    return content, usage


def _pool_node_name(pool) -> str:
    if pool is None:
        return "single"
    try:
        return getattr(pool, "last_node", "") or "pool"
    except Exception:
        return "unknown"


_LEDGER_LOCK = Lock()


def _ledger_write(step, model, node, elapsed_ms, prompt_tokens="unknown",
                  completion_tokens="unknown", retries=0, error="", role="judgement") -> None:
    """耗时账本落盘：lib 取 ASSET_LIB / ASSET_LIB_DIR；未设置则跳过（不入账）。"""
    lib = os.environ.get("ASSET_LIB") or os.environ.get("ASSET_LIB_DIR")
    if not lib:
        return
    try:
        from engine import perf_ledger
        with _LEDGER_LOCK:
            perf_ledger.append(Path(lib), {
                "step": step, "model": model, "node": node,
                "elapsed_ms": elapsed_ms, "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens, "retries": retries, "error": error,
                "role": role,
            })
    except Exception:
        pass                      # 账本失败不影响主流程


def _call_single(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int | None = None,
    retries: int = 3,
    temperature: float = 0.4,
    min_len: int = 0,
) -> tuple[str, dict[str, Any]]:
    """单端点原路径（无池配置时行为与历史版本字节级一致）。"""
    payload: dict[str, Any] = {
        "model": API_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if thinking_disabled():
        payload["thinking"] = {"type": "disabled"}
    last_err: BaseException | None = None
    started = time.time()
    for attempt in range(retries):
        try:
            resp = requests.post(
                API_URL,
                json=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
                timeout=(30, 600),
                stream=True,
            )
            if resp.status_code == 429:
                resp.close()
                last_err = RuntimeError(f"持续 429 限流（等待后仍限流，{retries} 次重试后放弃）")
                wait = min(60, 10 * (2 ** attempt))
                log(f"    ⏳ 429 限流，等待 {wait}s 重试...")
                time.sleep(wait)
                continue
            if resp.status_code in (401, 403):
                resp.close()
                raise LLMAuthError(
                    f"API 认证失败（HTTP {resp.status_code}）——请检查 OPENAI_API_KEY / SUMMARY_API_URL 配置"
                )
            resp.raise_for_status()
            parts, usage, finish_reason = consume_sse(resp)
            content = "".join(parts).strip()
            if not content:
                raise ValueError("流式响应为空")
            if min_len and len(content) < min_len:
                raise ValueError(f"输出过短（{len(content)} 字符 < {min_len}）——LLM 误服从结尾句指令，重试")
            if finish_reason == "length":
                raise ValueError(f"输出截断（finish_reason=length，已输出 {len(content)} 字符；可调大 max_tokens）")
            if finish_reason not in (None, "stop"):
                raise ValueError(f"异常结束（finish_reason={finish_reason}）")
            # 可观测性：输入/输出/有效落盘字符、总耗时、缓存命中（绝不含密钥/授权头）
            # 工单 9：用量走统一归一化——端点不返回的字段记 unknown，不猜；
            # 归一化结果上再盖回端点原始字段与既有键，保持向后兼容。
            raw_usage = dict(usage or {})
            input_chars = len(system_prompt) + len(user_prompt)
            normalized = normalize_usage(raw_usage, input_chars=input_chars, landed_chars=len(content))
            normalized.update(raw_usage)
            normalized["cache_hit"] = bool(raw_usage.get("cache_hit", False))
            normalized["input_chars"] = input_chars
            normalized["output_chars"] = len(content)
            normalized["elapsed_ms"] = int((time.time() - started) * 1000)
            normalized["attempts"] = attempt + 1
            return content, normalized
        except LLMAuthError:
            raise
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 400 and "stream_options" in payload:
                payload.pop("stream_options", None)
                log("    ℹ️ API 不支持 include_usage，降级重试（token 统计不可用）")
                continue
            last_err = e
            if e.response is not None:
                try:
                    e.response.close()
                except Exception:
                    pass
            wait = 5 * (attempt + 1)
            log(f"    ⚠️ 调用失败({e})，{wait}s 后重试 ({attempt+1}/{retries})")
            time.sleep(wait)
        except Exception as e:
            last_err = e
            wait = 5 * (attempt + 1)
            log(f"    ⚠️ 调用失败({e})，{wait}s 后重试 ({attempt+1}/{retries})")
            time.sleep(wait)
    if last_err is None:
        last_err = RuntimeError("API 调用失败（重试耗尽）")
    raise last_err
