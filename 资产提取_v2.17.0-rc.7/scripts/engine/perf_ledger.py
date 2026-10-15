# -*- coding: utf-8 -*-
"""调用耗时账本（加速改法工单 T5 / 设计方案 §3.3）。

- 一行一条 JSONL：`{"ts","step","model","node","elapsed_ms","prompt_tokens",
  "completion_tokens","retries","error"}`。
- **白名单字段**写盘：`prompt` 之类的额外键直接丢弃——正文、原文、密钥绝不入账本。
- token 端点不返回时写 `"unknown"`（不猜）。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Mapping

LEDGER_NAME = "耗时账本.jsonl"

# 白名单：只有这些键允许落盘（顺序即输出顺序）
_FIELDS = ("step", "model", "node", "elapsed_ms", "prompt_tokens",
           "completion_tokens", "retries", "error")

# F-19：字段白名单只挡得住「多写的键」，挡不住 error 串里夹带的凭据值。
# 打码只吃掉命中的值本身，错误码（401/429）、阶段名、节点名照原样留着——
# 排障要靠这些上下文，把它们清空等于用一个盲区换另一个盲区。
_REDACTED = "«已脱敏:凭据形状»"
_SECRET_VALUE = re.compile(
    r"(?:(?:sk|pk|api)[-_][A-Za-z0-9_\-]{8,}"
    r"|AIza[0-9A-Za-z_\-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{8,}"
    r"|xox[aprs]-[A-Za-z0-9_\-]{8,}"
    r"|AKIA[0-9A-Z]{8,}"
    r"|eyJ[A-Za-z0-9_\-]{8,})")
_SECRET_KV = re.compile(
    r"(?i)(\b(?:api[_-]?key|secret|token|password|authorization|x-goog-api-key|bearer)\b"
    r"\s*[:=]\s*)(?:\"[^\"]+\"|'[^']+'|\S{6,})")


def redact_error(text) -> str:
    """把错误串里的凭据形状就地打码；非字符串与空值原样返回。"""
    if not isinstance(text, str) or not text:
        return text or ""
    masked = _SECRET_KV.sub(lambda m: m.group(1) + _REDACTED, text)
    masked = _SECRET_VALUE.sub(_REDACTED, masked)
    return masked


def _lib_dir(lib) -> Path:
    return Path(lib) / ".process"


def _to_int(value) -> int:
    """prompt_tokens 等可能是 "unknown"——入账本时为 0，不猜数。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _emit_unified(data: Mapping) -> None:
    """C8 薄适配层：把本次调用补记一条到跨技能统一账本（schemas/ledger.py 格式）。

    只吃白名单字段（正文/密钥天然不在其中）；导入失败、记录被拒等一切异常
    由调用方吞掉——绝不影响原耗时账本。UNIFIED_LEDGER_DISABLE=1 为总开关。
    """
    if os.environ.get("UNIFIED_LEDGER_DISABLE") == "1":
        return
    repo_root = Path(__file__).resolve().parents[2]
    if os.environ.get("UNIFIED_LEDGER_PY"):
        candidates = [Path(os.environ["UNIFIED_LEDGER_PY"])]
    else:
        # 同级 schemas/（组合安装）→ 包内受控副本（单独安装的收件人）
        candidates = [repo_root.parent / "schemas" / "ledger.py",
                      repo_root / "vendor" / "schemas" / "ledger.py"]
    ledger_py = next((c for c in candidates if c.is_file()), candidates[0])
    if not ledger_py.is_file():
        return
    import importlib.util

    spec = importlib.util.spec_from_file_location("_run_ledger_adapter", ledger_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ts = str(data.get("ts") or time.strftime("%Y-%m-%dT%H:%M:%S"))
    record = {
        "skill": "资产提取",
        "skill_version": os.environ.get("UNIFIED_SKILL_VERSION", "2.17.0-rc.5"),
        # 同一次跑批（同日同进程）同阶段后写覆盖，跨进程重跑不互相吞账
        "run_id": str(data.get("run_id") or f"perf-{ts[:10]}-{os.getpid()}"),
        "ts": ts,
        "stage": str(data.get("step") or "未命名"),
        "model_node": str(data.get("node") or data.get("model") or "未记录"),
        "input_units": 1,
        "calls": 1,
        "tokens_in": _to_int(data.get("prompt_tokens")),
        "tokens_out": _to_int(data.get("completion_tokens")),
        "cost_cny": 0.0,            # 本仓无计价来源，记 0 由总账聚合侧换算
        "gate_failures": 1 if data.get("error") else 0,
        "retries": _to_int(data.get("retries")),
        "artifact_paths": [],
    }
    out = Path(os.environ.get("UNIFIED_LEDGER_PATH")
               or repo_root / ".lemon" / "run-ledger.jsonl")
    mod.append(record, out)


def append(lib, row: Mapping) -> None:
    """把一行账目追加到 <lib>/.process/耗时账本.jsonl。非白名单键一律丢弃。"""
    data = dict(row)
    data.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
    out = {key: data.get(key, "" if key == "error" else "unknown") for key in _FIELDS}
    out["error"] = redact_error(out.get("error"))
    out["ts"] = data["ts"]
    if "role" in data:
        out["role"] = str(data["role"])   # T13 起账本带 role；旧行没有该字段
    path = _lib_dir(lib) / LEDGER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(out, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:  # C8 统一账本补记：失败静默，原账本行为永远优先
        _emit_unified({**data, "error": out["error"]})
    except Exception:
        pass


def summarize(lib) -> dict:
    """按 step 聚合：calls / total_ms / avg_ms / tokens / retries / failures。"""
    path = _lib_dir(lib) / LEDGER_NAME
    summary: dict[str, dict] = {}
    if not path.is_file():
        return summary
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue          # 半行/坏行忽略
            step = str(row.get("step") or "未命名")
            bucket = summary.setdefault(step, {
                "calls": 0, "total_ms": 0, "avg_ms": 0,
                "prompt_tokens": 0, "completion_tokens": 0,
                "retries": 0, "failures": 0, "by_role": {},
            })
            role = str(row.get("role") or "judgement")
            rb = bucket["by_role"].setdefault(role, {"calls": 0, "total_ms": 0})
            rb["calls"] += 1
            try:
                rb["total_ms"] += int(row.get("elapsed_ms") or 0)
            except (TypeError, ValueError):
                pass
            bucket["calls"] += 1
            try:
                bucket["total_ms"] += int(row.get("elapsed_ms") or 0)
            except (TypeError, ValueError):
                pass
            for key in ("prompt_tokens", "completion_tokens"):
                try:
                    bucket[key] += int(row.get(key) or 0)
                except (TypeError, ValueError):
                    bucket[key] = "unknown"
            try:
                bucket["retries"] += int(row.get("retries") or 0)
            except (TypeError, ValueError):
                pass
            if row.get("error"):
                bucket["failures"] += 1
    for bucket in summary.values():
        if bucket["calls"]:
            bucket["avg_ms"] = round(bucket["total_ms"] / bucket["calls"])
    return summary
