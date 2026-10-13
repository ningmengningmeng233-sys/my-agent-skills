#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ledger.py — C8 跨技能统一成本/效果账本（零第三方依赖，仅 stdlib）。

- 格式由同目录 run-ledger.schema.json（draft-07）定义，校验/汇总都读 schema，不硬编码字段。
- 落盘 JSONL（UTF-8、ensure_ascii=False），幂等键 (skill, run_id, stage)：后写覆盖。
- 安全铁律：账本永不收录密钥/cookie/媒体 URL——键名命中 schema 的
  x-credential-key-pattern（api_key|token|secret|cookie|authorization|password，不分大小写）
  即拒绝；artifact_paths 里的 http(s) URL 即拒绝。报错信息不回显字段值。

用法：
    python ledger.py append --json '<record>' [--path <file.jsonl>]
    python ledger.py summarize [--root DIR]
退出码：0 正常；1 记录非法。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent / "run-ledger.schema.json"
DEFAULT_LEDGER = Path(__file__).resolve().parent.parent / ".lemon" / "run-ledger.jsonl"

_JSON_TYPES = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "array": (list,),
}


def _load_schema() -> dict:
    with open(SCHEMA_PATH, encoding="utf-8") as fp:
        return json.load(fp)


def _iter_keys(obj, prefix=""):
    """递归产出所有 dict 键（含嵌套），报错只报键路径不回显值。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k), f"{prefix}{k}"
            yield from _iter_keys(v, f"{prefix}{k}.")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _iter_keys(v, f"{prefix}[{i}].")


def validate(record) -> list[str]:
    """按 schema 校验一条记录，返回拒绝原因列表；空列表 = 合法。"""
    reasons: list[str] = []
    if not isinstance(record, dict):
        return ["记录必须是 JSON 对象"]
    schema = _load_schema()
    props = schema.get("properties", {})

    for field in schema.get("required", []):
        if field not in record:
            reasons.append(f"缺少必填字段: {field}")

    for field, spec in props.items():
        if field not in record:
            continue
        val = record[field]
        want = spec.get("type")
        types = _JSON_TYPES.get(want)
        if types and not isinstance(val, types):
            reasons.append(f"字段 {field} 类型应为 {want}，实际为 {type(val).__name__}")
            continue
        if want == "integer" and isinstance(val, bool):
            reasons.append(f"字段 {field} 必须为整数")
        if want in ("number", "integer") and isinstance(val, (int, float)):
            if "minimum" in spec and val < spec["minimum"]:
                reasons.append(f"字段 {field} 不得为负（minimum={spec['minimum']}），实际 {val}")
        if want == "string" and "minLength" in spec and len(val) < spec["minLength"]:
            reasons.append(f"字段 {field} 不得为空")
        if want == "array":
            item_t = (spec.get("items") or {}).get("type")
            for i, item in enumerate(val):
                if item_t and not isinstance(item, _JSON_TYPES.get(item_t, (object,))):
                    reasons.append(f"字段 {field}[{i}] 类型应为 {item_t}")
                    continue
                if item_t == "string" and len(item) < (spec.get("items") or {}).get("minLength", 0):
                    reasons.append(f"字段 {field}[{i}] 不得为空字符串")

    # 凭证形键名：schema 已声明的合法字段豁免（如 tokens_in），其余一律拒绝
    cred_re = re.compile(schema.get("x-credential-key-pattern", r"(?i)(api_key|token|secret|cookie|authorization|password)"))
    for key, path in _iter_keys(record):
        if cred_re.search(key) and path not in props:
            reasons.append(f"疑似凭证字段，账本拒绝收录: {path}")

    # 媒体 URL：仅出现在产物字段即拒绝（不区分大小写）
    url_re = re.compile(schema.get("x-url-forbidden-pattern", r"(?i)^(http://|https://)"))
    for field in schema.get("x-url-forbidden-array-fields", []):
        values = record.get(field)
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, str) and url_re.search(item):
                reasons.append(f"字段 {field} 不得含媒体 URL（已脱敏，不回显原值）")
    return reasons


def _read_lines(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue  # 坏行跳过，永不炸账本
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def append(record, path=None) -> None:
    """校验 → 按 (skill, run_id, stage) 幂等去重（后写覆盖）→ 追加写回 JSONL。"""
    reasons = validate(record)
    if reasons:
        raise ValueError("；".join(reasons))
    p = Path(path) if path else DEFAULT_LEDGER
    p.parent.mkdir(parents=True, exist_ok=True)
    key = (record.get("skill"), record.get("run_id"), record.get("stage"))
    rows = [r for r in _read_lines(p)
            if (r.get("skill"), r.get("run_id"), r.get("stage")) != key]
    rows.append(record)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fp:
        for r in rows:
            fp.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, p)


def _as_num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def summarize(paths=None) -> dict:
    """合并多个 JSONL（可跨技能），回答三问：花多少钱 / 返工几次 / 哪个门最费。"""
    if not paths:
        files = [DEFAULT_LEDGER]
    else:
        items = paths if isinstance(paths, (list, tuple)) else [paths]
        files = []
        for item in items:
            p = Path(item)
            files.extend(sorted(p.rglob("*.jsonl")) if p.is_dir() else [p])

    per_skill: dict[str, dict] = {}
    per_stage: dict[str, float] = {}
    total_cost = 0.0
    total_reworks = 0
    total_calls = 0
    rewrite_episodes = 0
    for f in files:
        for rec in _read_lines(f):
            if validate(rec):
                continue  # 脏记录不进汇总
            cost = float(rec.get("cost_cny") or 0)
            calls = _as_num(rec.get("calls"))
            retries = _as_num(rec.get("retries"))
            skill = str(rec.get("skill"))
            b = per_skill.setdefault(skill, {"records": 0, "cost_cny": 0.0, "tokens": 0,
                                             "calls": 0, "retries": 0, "gate_failures": 0})
            b["records"] += 1
            b["cost_cny"] += cost
            b["tokens"] += _as_num(rec.get("tokens_in")) + _as_num(rec.get("tokens_out"))
            b["calls"] += calls
            b["retries"] += retries
            b["gate_failures"] += _as_num(rec.get("gate_failures"))
            total_cost += cost
            total_reworks += retries
            total_calls += calls
            if retries > 0:
                rewrite_episodes += 1
            stage = str(rec.get("stage"))
            per_stage[stage] = per_stage.get(stage, 0.0) + cost
    best = max(per_stage.items(), key=lambda kv: kv[1]) if per_stage else (None, 0.0)
    for b in per_skill.values():
        b["cost_cny"] = round(b["cost_cny"], 6)
    return {
        "skills": per_skill,
        "total_cost_cny": round(total_cost, 6),
        "total_reworks": total_reworks,
        "total_calls": total_calls,
        "rewrite_episodes": rewrite_episodes,
        "most_expensive_gate": {"stage": best[0], "cost_cny": round(best[1], 6)},
    }


def import_production_log(text, skill="小说转短剧", skill_version="1.5.0") -> list[dict]:
    """把 小说转短剧_v1.5.0 的白话生产小结 production_log.md 转成统一账本记录。

    只读解析表格行「| 第 N 集 | 状态 | 执笔模型 | 回炉次数 | 备注 |」；
    该日志无 token/金额维度 → cost_cny 记 0，retries = 回炉次数，
    备注里「经 X 次回炉自愈后通过」计入 gate_failures。
    """
    m = re.search(r"生产记录\s*——\s*《(.+?)》", text)
    run_id = m.group(1) if m else "production_log"
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    records = []
    row_re = re.compile(r"\|\s*第\s*(\d+)\s*集\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|")
    for mm in row_re.finditer(text):
        ep, _status, node, retries, note = (g.strip() for g in mm.groups())
        gm = re.search(r"经\s*(\d+)\s*次回炉自愈后通过", note)
        node = node or "-"
        records.append({
            "skill": skill,
            "skill_version": skill_version,
            "run_id": run_id,
            "ts": now,
            "stage": f"第{ep}集写作",
            "model_node": "未记录" if node in ("-", "—", "") else node,
            "input_units": 1,
            "calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_cny": 0,
            "gate_failures": int(gm.group(1)) if gm else 0,
            "retries": _as_num(retries),
            "artifact_paths": [],
        })
    return records


def _render_table(summary: dict) -> str:
    lines = [f"{'技能':<10}\t记录\t成本(元)\tToken\t调用\t返工\t门禁失败"]
    for skill, b in summary["skills"].items():
        lines.append(f"{skill:<10}\t{b['records']}\t{b['cost_cny']:.2f}\t{b['tokens']}\t{b['calls']}\t{b['retries']}\t{b['gate_failures']}")
    gate = summary["most_expensive_gate"]
    lines.append("")
    lines.append(f"总成本：{summary['total_cost_cny']:.2f} 元 ｜ 返工：{summary['total_reworks']} 次"
                 f"（返工集/条目 {summary['rewrite_episodes']} 个）｜ 调用：{summary['total_calls']} 次")
    lines.append(f"最贵的门/阶段：{gate['stage'] or '（无）'}（{gate['cost_cny']:.2f} 元）")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="跨技能统一成本/效果账本")
    sub = parser.add_subparsers(dest="cmd", required=True)
    pa = sub.add_parser("append", help="追加一条记录")
    pa.add_argument("--json", required=True, help="JSON 记录")
    pa.add_argument("--path", default=None, help=f"JSONL 路径（默认 {DEFAULT_LEDGER}）")
    ps = sub.add_parser("summarize", help="汇总多个账本")
    ps.add_argument("--root", default=None, help="扫描目录下全部 *.jsonl（默认读总账路径）")
    args = parser.parse_args(argv)

    if args.cmd == "append":
        try:
            record = json.loads(args.json)
        except ValueError as e:
            print(f"JSON 解析失败: {e}", file=sys.stderr)
            return 1
        try:
            append(record, args.path)
        except ValueError as e:
            print(f"账本记录被拒绝: {e}", file=sys.stderr)
            return 1
        print("已入账")
        return 0

    paths = [args.root] if args.root else None
    print(_render_table(summarize(paths)))
    return 0


if __name__ == "__main__":
    # 输出编码是产品事实：账本全是中文，不能让调用方 shell 的 cp936 决定落到管道的字节
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
