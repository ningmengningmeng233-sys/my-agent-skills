#!/usr/bin/env python3
"""凭证协议校验器（D1）：读 `references/protocol/verification-protocol.json` 并拒绝假绿灯。

红线三条：
1. `status=PASS` 必须带日期与产物路径，且产物文件真实存在——否则这条 PASS 无凭据，判红；
2. `BLOCKED-CRED` 必须写清原因 + 解锁条件 + 复核命令，不许只写「做不了」；
3. 协议文件只登记取用方式，任何形似真实密钥的字面量都判红（本文件永不入库真 key）。

退出码：0 合法 / 2 校验不通过（白话原因，不抛栈）/ 3 协议文件缺失。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from closure_gate_contract import GATE_ORDER  # noqa: E402  档位单一事实源，禁止平行登记

REPO = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = REPO / "references" / "protocol" / "verification-protocol.json"

CHANNEL_FIELDS = ("name", "key_env", "key_value_policy", "where_to_get", "recheck_command")
STATUSES = ("PASS", "FAIL", "NOT_RUN", "BLOCKED-CRED", "REVIEW_REQUIRED")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: 形似真实凭证的字面量：sk- 前缀、32/40 位十六进制、20+ 位字母数字混合串
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{10,}"
    r"|\b[0-9a-f]{32,40}\b"
    r"|\b(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{20,}\b)"
)


def _iter_strings(obj, path=""):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _iter_strings(v, f"{path}[{i}]")


def _nonempty(rec, field):
    val = rec.get(field)
    return isinstance(val, str) and val.strip() != ""


def _surface(repo_root: Path):
    """每次现读清单：测试会就地改档位与裁剪声明，缓存会给出假绿灯。"""
    from release_surface import ReleaseSurface
    return ReleaseSurface(Path(repo_root))


def validate(doc, repo_root: Path = REPO) -> list[str]:
    reasons: list[str] = []
    if not isinstance(doc, dict):
        return ["协议文件必须是 JSON 对象"]
    if doc.get("schema") != "h3-verification-protocol/1":
        reasons.append(f"schema 不受支持: {doc.get('schema')!r}")
    if not DATE_RE.match(str(doc.get("updated", ""))):
        reasons.append("顶层 updated 必须是 YYYY-MM-DD")

    for i, ch in enumerate(doc.get("channels") or []):
        for f in CHANNEL_FIELDS:
            if not _nonempty(ch, f):
                reasons.append(f"channels[{i}] 缺少必填字段 {f}")
        if ch.get("key_value_policy") != "REPLACE":
            reasons.append(f"channels[{i}] key_value_policy 必须是 REPLACE（协议文件不存真 key）")

    records = doc.get("records") or []
    if not records:
        reasons.append("records 为空：六档验证层级必须逐档登记状态")
    seen: set[str] = set()
    for i, rec in enumerate(records):
        gate = rec.get("gate")
        if gate not in GATE_ORDER:
            reasons.append(f"records[{i}] 未知档位 gate={gate!r}")
        elif gate in seen:
            reasons.append(f"records[{i}] 档位 {gate} 重复登记")
        else:
            seen.add(gate)
        status = rec.get("status")
        if status not in STATUSES:
            reasons.append(f"records[{i}]({gate}) status 非法: {status!r}")
            continue
        if not _nonempty(rec, "reason") and status in ("FAIL", "NOT_RUN", "BLOCKED-CRED"):
            reasons.append(f"records[{i}]({gate}) {status} 必须写明 reason")
        if status == "BLOCKED-CRED":
            for f in ("unlock_condition", "recheck_command"):
                if not _nonempty(rec, f):
                    reasons.append(f"records[{i}]({gate}) BLOCKED-CRED 必须写 {f}")
        if status == "PASS":
            if not DATE_RE.match(str(rec.get("date", ""))):
                reasons.append(f"records[{i}]({gate}) 声称 PASS 但没有日期（date 必须是 YYYY-MM-DD）")
            paths = rec.get("artifact_paths")
            if not isinstance(paths, list) or not paths:
                reasons.append(f"records[{i}]({gate}) 声称 PASS 但没有产物路径")
                continue
            if not _nonempty(rec, "note"):
                reasons.append(f"records[{i}]({gate}) PASS 记录须用 note 说明产物证明了什么")
            for p in paths:
                if not isinstance(p, str) or not p:
                    reasons.append(f"records[{i}]({gate}) 产物路径为空")
                elif not (repo_root / p).is_file():
                    # 缺件不放行：只有「本档声明裁剪 + 证据索引里存着它的 sha256」才算过，
                    # 在册文件被删或压根没声明过都继续报错（见 release_surface.py）。
                    ok, why = _surface(repo_root).check(p)
                    if not ok:
                        reasons.append(f"records[{i}]({gate}) 产物文件不存在: {p} —— {why}")
            # 源码存在不等于跑过：PASS 必须指向一次真实采集留下的运行日志
            if not any(isinstance(p, str) and p.startswith("evidence/") for p in paths):
                reasons.append(f"records[{i}]({gate}) 声称 PASS 但没有运行证据："
                               f"artifact_paths 至少要有一条落在 evidence/ 下"
                               f"（collect_closure_evidence 产出的索引或日志）")

    for gate in GATE_ORDER:
        if gate not in seen:
            reasons.append(f"档位 {gate} 未登记状态：六档必须逐档登记，留白即视为回避")

    for path, val in _iter_strings(doc):
        # 只按字段名精确匹配 key_value（键值本身），不得误伤合法字段 key_value_policy
        if path.split(".")[-1].split("[")[0] == "key_value":
            reasons.append(f"协议文件不得存密钥值: {path}")
        elif SECRET_RE.search(val):
            reasons.append(f"疑似真实密钥字面量，协议文件拒绝收录: {path}")
    return reasons


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "check":
        argv = argv[1:]
    if not PROTOCOL_PATH.is_file():
        print(f"找不到凭证协议文件：{PROTOCOL_PATH}", file=sys.stderr)
        return 3
    try:
        doc = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"凭证协议文件不是合法 JSON：{exc}", file=sys.stderr)
        return 2
    reasons = validate(doc)
    if reasons:
        print("凭证协议校验不通过：", file=sys.stderr)
        for r in reasons:
            print(f"  - {r}", file=sys.stderr)
        return 2
    print(f"凭证协议校验通过：{len(doc.get('channels') or [])} 条渠道、"
          f"{len(doc.get('records') or [])} 条验证记录（协议文件不含任何真实密钥）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
