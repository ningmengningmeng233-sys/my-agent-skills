#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2_structure.py - textlint 结构候选桥的 Python 侧（alpha.1 P04）。

坐标合同（设计 4 节）：

  - Node 侧（textlint AST 遵循 ECMAScript）输出 **UTF-16 码元偏移**；
    本模块把它们显式映射到 **Python 字符偏移**（非 BMP 码点——emoji 等——
    在 UTF-16 里占 2 个码元，映射表逐码点构建）。
  - 每个候选回读原文验证：切片结果必须等于锚点 excerpt；对不上就
    ``StructureError``，绝不返回可能错位的候选或空 PASS。
  - 重复原文用 ``occurrence_index`` 区分第 N 次出现，不猜位置。
  - 扫描是纯函数：不改源文本、不写源文件。

桥接进程：``node runtime/textlint/bridge.mjs``，stdin/stdout JSON，
schema 钉死 ``lieflat-textlint-bridge-v2``；工作目录固定在
runtime/textlint（node_modules 与包内规则），用户 textlintrc 不可见。
v2 相对 v1：finding 增加可选 ``context_range_utf16``（重复句的首次出现参照、
时间链的所在行上下文），缺字段/range 形状非法/回读失配一律显式报错。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import v2_contracts as vc

BRIDGE_SCHEMA = "lieflat-textlint-bridge-v2"
PREFLIGHT_SCHEMA = "lieflat-textlint-preflight-v1"
SELFCHECK_SCHEMA = "lieflat-textlint-selfcheck-v1"
NPM_EXE = "npm"  # Windows 上是 npm.cmd，必须经 shutil.which 解析后再调用
BRIDGE_DIR = Path(__file__).resolve().parent.parent / "runtime" / "textlint"
BRIDGE_SCRIPT = BRIDGE_DIR / "bridge.mjs"
NODE_EXE = "node"
_BRIDGE_TIMEOUT = 60

# 检测规则 ID → 十三类稳定问题 ID（补正方案 6 节：rule_id 与 kind 不混为同一
# 字段；映射与 references/v2-candidate-rules-zh.md 的目录一一对应）。
_KIND_BY_RULE = {
    "zh-repeated-sentence": "SEMANTIC_ECHO",
    "zh-mechanical-temporal-chain": "MECHANICAL_TEMPORAL_CHAIN",
}


class StructureError(RuntimeError):
    """结构扫描失败（Node/依赖缺失、桥崩溃、schema 不符或坐标回读失败）。

    依赖没准备好也必须走这里：不能退化成「扫出 0 个候选」——那是把能力缺失
    粉饰成稿件干净。
    """


def _utf16_to_py_index(text: str) -> dict[int, int]:
    """UTF-16 码元偏移 → Python 字符偏移映射表（含右端点 len*2 溢出格）。"""
    mapping: dict[int, int] = {}
    utf16 = 0
    for py, ch in enumerate(text):
        mapping[utf16] = py
        utf16 += 2 if ord(ch) > 0xFFFF else 1
    mapping[utf16] = len(text)
    return mapping


def required_modules(bridge_dir=None) -> tuple:
    """必需依赖按 ``runtime/textlint/package.json`` 声明列，不在 Python 里另抄一份。"""
    root = Path(bridge_dir) if bridge_dir else BRIDGE_DIR
    declared = json.loads((root / "package.json").read_text(encoding="utf-8"))
    return tuple(sorted(declared.get("dependencies") or {}))


def _self_check(root) -> dict:
    """问桥自己：这些依赖到底从哪儿加载的、Node 是多少。"""
    try:
        proc = subprocess.run([NODE_EXE, str(Path(root) / "bridge.mjs"), "--self-check"],
                              cwd=str(root), capture_output=True, timeout=_BRIDGE_TIMEOUT,
                              env=_clean_node_env())
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        payload = json.loads(proc.stdout.decode("utf-8"))
    except ValueError:
        return {}
    if payload.get("schema") != SELFCHECK_SCHEMA:
        return {}
    return payload


def _clean_node_env() -> dict:
    """把 NODE_PATH / NODE_MODULES 这类外部注入清掉：证明加载只来自本地 node_modules。"""
    import os
    env = dict(os.environ)
    for key in ("NODE_PATH", "NODE_MODULES"):
        env.pop(key, None)
    return env


def preflight(*, bridge_dir=None) -> dict:
    """结构扫描能力是否真的就绪：Node、锁、每个必需依赖的解析位置。

    ``ready=False`` 时调用方必须停下并报缺失；``resolved_modules`` 用来证明
    加载来自哪一棵树——借开发树不算包内独立运行。
    """
    root = Path(bridge_dir) if bridge_dir else BRIDGE_DIR
    node = shutil.which(NODE_EXE)
    lock = root / "package-lock.json"  # 锁文件是唯一依赖版本权威，缺它就不算就绪
    modules = required_modules(root) if (root / "package.json").is_file() else ()
    missing = [name for name in modules if not (root / "node_modules" / name).is_dir()]
    report = {"schema": PREFLIGHT_SCHEMA, "bridge_dir": str(root), "node": node,
              "required_modules": list(modules), "missing_modules": missing,
              "lock_sha256": (hashlib.sha256(lock.read_bytes()).hexdigest()
                              if lock.is_file() else None),
              "node_version": None, "resolved_modules": {}, "versions": {},
              "prepare_command": f'npm ci --prefix "{root}"'}
    report["ready"] = bool(node) and lock.is_file() and not missing
    report["resolved_outside_tree"] = []
    if report["ready"]:
        checked = _self_check(root)
        report["resolved_modules"] = checked.get("resolved") or {}
        report["node_version"] = checked.get("node")
        report["versions"] = checked.get("versions") or {}
        own = root / "node_modules"
        for name in modules:
            path = report["resolved_modules"].get(name)
            if not path:
                continue
            try:
                Path(path).resolve().relative_to(own.resolve())
            except (ValueError, OSError):
                # 借了别的树（开发树也算）：本树就没独立就绪，必须显式报出来
                report["resolved_outside_tree"].append(name)
        if (report["resolved_outside_tree"]
                or len(report["resolved_modules"]) < len(modules)):
            report["ready"] = False
            report["missing_modules"] = sorted(
                set(report["missing_modules"]) | set(report["resolved_outside_tree"])
                | {m for m in modules if m not in report["resolved_modules"]})
    return report


def prepare_runtime_copy(candidate_root, dest) -> dict:
    """把候选复制成可写的**依赖准备运行副本**，按 package-lock 装锁定版本。

    干净发行候选（不含 node_modules）与准备后的运行副本是两棵树：登记要分开，
    副本里多出来的依赖文件不能写成「零漂移」。
    """
    src, dst = Path(candidate_root), Path(dest)
    if dst.exists():
        raise StructureError(f"运行副本目标已存在，不覆盖: {dst}")
    npm = shutil.which(NPM_EXE)
    if npm is None:
        # 指令按副本将来所在位置给出：这里不能引用还没算出来的 runtime，
        # 否则「没有 npm」这条路径自己先炸，能力未准备的说明反而拿不到。
        raise StructureError(
            "未找到 npm，无法按锁准备依赖：结构扫描能力保持未准备"
            f"（指令：npm ci --prefix "
            f"\"{dst / 'runtime' / 'textlint'}\"）")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst,
                    ignore=shutil.ignore_patterns("node_modules", "__pycache__",
                                                  "*.lieflat-work", ".git"))
    runtime = dst / "runtime" / "textlint"
    before = preflight(bridge_dir=runtime)
    # Windows 上 npm 是 .cmd：必须用 which 解析出的完整路径，裸 "npm" 起不来
    proc = subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=str(runtime),
                          capture_output=True, timeout=1800, env=_clean_node_env())
    after = preflight(bridge_dir=runtime)
    added = sum(1 for item in (runtime / "node_modules").rglob("*") if item.is_file()) \
        if (runtime / "node_modules").is_dir() else 0
    if proc.returncode != 0 or not after["ready"]:
        raise StructureError("依赖准备失败，结构扫描能力保持未准备（不静默降级为空候选）："
                             f"{proc.stderr.decode('utf-8', 'replace')[:300]}；"
                             f"仍缺 {after['missing_modules']}")
    return {"clean_candidate": str(src), "run_copy": str(dst),
            "clean_missing_modules": before["missing_modules"],
            "candidate_lock_sha256": before["lock_sha256"],
            "run_copy_lock_sha256": after["lock_sha256"],
            "node_version": after["node_version"],
            "dependency_versions": after["versions"],
            "resolved_modules": after["resolved_modules"],
            "run_copy_files_added": added}


def call_bridge(text: str, *, scene: str, bridge_dir=None) -> dict:
    """调用 Node 桥并返回解析后的 JSON（schema 校验在这里钉死）。

    先 preflight：Node 或依赖没就绪就显式报错并给出按锁重建的指令。
    """
    check = preflight(bridge_dir=bridge_dir)
    if not check["ready"]:
        raise StructureError(
            "textlint 依赖未准备，结构扫描停止：Node="
            f"{check['node'] or '缺失'}，缺依赖 {check['missing_modules'] or '（锁文件缺失）'}；"
            f"准备指令：{check['prepare_command']}")
    if shutil.which(NODE_EXE) is None and NODE_EXE == "node":
        raise StructureError("未找到 Node 运行时（textlint 桥需要 Node>=20.18.0）："
                             "结构扫描不能继续，也不得静默返回空结果")
    request = json.dumps({"text": text, "scene": scene},
                         ensure_ascii=False).encode("utf-8")
    try:
        proc = subprocess.run(
            [NODE_EXE, str(Path(bridge_dir or BRIDGE_DIR) / "bridge.mjs")],
            input=request, cwd=str(bridge_dir or BRIDGE_DIR), capture_output=True,
            timeout=_BRIDGE_TIMEOUT, env=_clean_node_env())
    except FileNotFoundError as exc:
        raise StructureError(
            f"Node 运行时不可用（{NODE_EXE}）：结构扫描不能继续，"
            "也不得静默返回空结果") from exc
    except subprocess.TimeoutExpired as exc:
        raise StructureError(f"textlint 桥超时（>{_BRIDGE_TIMEOUT}s）: {exc}") from exc
    if proc.returncode != 0:
        raise StructureError(
            "textlint 桥失败（exit "
            f"{proc.returncode}）：{proc.stderr.decode('utf-8', 'replace')[:400]}")
    try:
        payload = json.loads(proc.stdout.decode("utf-8"))
    except ValueError as exc:
        raise StructureError(f"textlint 桥输出不是合法 JSON: {exc}") from exc
    if payload.get("schema") != BRIDGE_SCHEMA:
        raise StructureError(
            f"textlint 桥 schema 不符: {payload.get('schema')!r} ≠ {BRIDGE_SCHEMA!r}")
    return payload


def _anchor_at(text: str, mapping: dict[int, int], text_sha: str,
               file_id: str, start16: int, end16: int) -> vc.SourceAnchor:
    """按 UTF-16 区间构造可回读锚点（区间非法/回读失配显式报错）。"""
    if not isinstance(start16, int) or not isinstance(end16, int) \
            or isinstance(start16, bool) or isinstance(end16, bool):
        raise StructureError(f"桥接坐标必须是整数: [{start16!r}, {end16!r}]")
    if start16 not in mapping or end16 not in mapping:
        raise StructureError(
            f"桥接坐标越界或未对齐码点边界: [{start16!r}, {end16!r}]")
    start, end = mapping[start16], mapping[end16]
    if start >= end:
        raise StructureError(f"桥接坐标区间为空: [{start16!r}, {end16!r}]")
    excerpt = text[start:end]
    occurrence = text[:start].count(excerpt)
    return vc.SourceAnchor(
        segment_id=file_id, start=start, end=end, excerpt=excerpt,
        text_sha256=text_sha, occurrence_index=occurrence)


def scan_structure(text: str, *, file_id: str, scene: str,
                   bridge_dir=None) -> tuple[vc.CandidateRecord, ...]:
    """扫描结构候选：Node 规则发现 → 坐标换算 → 回读验证 → 候选合同。

    候选 kind 使用十三类稳定 ID（``_KIND_BY_RULE``），rule_id 保留检测规则
    ID；重复句候选携带首次出现的参照锚点，时间链候选携带所在行上下文锚点
    ——全部锚点都经回读验证，后续裁判不必只凭一个连接词猜场景。
    """
    payload = call_bridge(text, scene=scene, bridge_dir=bridge_dir)
    if payload.get("schema") != BRIDGE_SCHEMA:
        raise StructureError(
            f"textlint 桥 schema 不符: {payload.get('schema')!r} ≠ {BRIDGE_SCHEMA!r}")
    mapping = _utf16_to_py_index(text)
    text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    candidates: list[vc.CandidateRecord] = []
    findings = payload.get("findings")
    if findings is None or not isinstance(findings, list):
        raise StructureError("textlint 桥输出缺 findings 列表")
    for idx, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise StructureError(f"桥接 finding 必须是映射: {finding!r}")
        rule_id = finding.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise StructureError(f"桥接 finding 缺 rule_id: {finding!r}")
        if rule_id not in _KIND_BY_RULE:
            raise StructureError(f"未登记的检测规则 ID: {rule_id!r}")
        range16 = finding.get("range_utf16")
        if not isinstance(range16, (list, tuple)) or len(range16) != 2:
            raise StructureError(f"桥接 finding 缺 range_utf16: {finding!r}")
        anchor = _anchor_at(text, mapping, text_sha, file_id,
                            range16[0], range16[1])
        context_anchors: list[vc.SourceAnchor] = []
        context_range16 = finding.get("context_range_utf16")
        if context_range16 is not None:
            if not isinstance(context_range16, (list, tuple)) \
                    or len(context_range16) != 2:
                raise StructureError(
                    f"桥接 context_range_utf16 形状非法: {context_range16!r}")
            context_anchors.append(
                _anchor_at(text, mapping, text_sha, file_id,
                           context_range16[0], context_range16[1]))
        context_blob = "\x00".join(a.excerpt for a in context_anchors) \
            if context_anchors else anchor.excerpt
        context_hash = hashlib.sha256(context_blob.encode("utf-8")).hexdigest()
        candidate = vc.CandidateRecord(
            candidate_id=f"cand-{file_id}-{idx:04d}",
            rule_id=rule_id,
            kind=_KIND_BY_RULE[rule_id],
            source_anchor=anchor,
            context_anchors=tuple(context_anchors),
            discovery_source="textlint",
            source_hash=text_sha, context_hash=context_hash,
            retrieval_score=None)
        candidates.append(candidate)
    return tuple(candidates)
