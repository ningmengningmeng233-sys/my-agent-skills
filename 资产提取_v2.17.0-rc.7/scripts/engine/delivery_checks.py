#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""签发门扩展检查（工单 13）。

`maintenance.py verify` 的机械检查在 v2.17 从「三张表齐全」扩展到可核验编译器事实：

- check_asset_coverage    数量守恒：正式资产 = 已产基础提示词 + 明确待审 + 明确阻断；
- check_art_constitution  美术总则指纹：文件存在即必须自洽，手改后不得继续生效；
- check_artifact_graph    依赖图完整：有总账就必须有依赖图，且节点/边不悬空；
- check_required_prompts  所有必需提示词：索引资产的基础提示词 + 调度清单的衍生节；
- check_visual_review     视觉审核：**仅当产品结果含生图**（direct_generate）时参与签发；
- check_package_files     包内文件：交付清单登记的产物必须在盘且哈希一致。

契约边界（规格 9）：data_only / prompt_only 下视觉审核**不适用**，资料线不读视觉门；
需要视觉事实的检查一律惰性导入，视觉模块不可用时只让本项失败，不拖垮资料线。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

# 名称是签发契约的一部分：必须与 pipeline_state.REQUIRED_DELIVERY_CHECKS 逐字一致
EXTENDED_DELIVERY_CHECKS: Tuple[str, ...] = (
    "check_asset_coverage",
    "check_art_constitution",
    "check_artifact_graph",
    "check_required_prompts",
    "check_visual_review",
    "check_package_files",
)

# 交付包必备文件（相对资产库根）：缺任一即「包不完整」
PACKAGE_REQUIRED = (
    "反推_最终.md",
    ".process/资产索引.json",
)


def _coverage_module():
    """数量守恒门（脚本与包两种调用方式都要能解析）。"""
    import importlib

    for name in ("check_asset_coverage", "scripts.check_asset_coverage"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise ImportError("找不到 check_asset_coverage（数量守恒门）")


def coverage_report(lib: Path) -> Dict[str, Any]:
    """数量守恒的完整报告（漏斗/违规/覆盖率）；无总账也无索引时 applicable=False。"""
    mod = _coverage_module()
    ledger = mod.load_ledger_or_index(str(lib))
    if not ledger:
        return {"applicable": False, "coverage": 1.0, "violations": [],
                "counts": {"formal": 0, "prompted": 0, "pending_or_blocked": 0, "violations": 0},
                "funnel_by_type": {}, "exit_code": 0}
    report = mod.evaluate(ledger, mod.load_prompted_names(str(lib)), mod.load_skipped(str(lib)))
    report["applicable"] = True
    return report


def check_asset_coverage(lib: Path) -> Tuple[int, str]:
    """数量守恒：没有产物、也没有明确原因的正式资产一律违规。"""
    report = coverage_report(lib)
    if not report.get("applicable"):
        return 0, "无总账也无资产索引：数量守恒不适用"
    c = report["counts"]
    detail = (f"正式 {c['formal']}｜已产提示词 {c['prompted']}｜待审/阻断 {c['pending_or_blocked']}"
              f"｜违规 {c['violations']}｜覆盖率 {report['coverage']:.1%}")
    if report["violations"]:
        names = "、".join(f"{v['asset_id']}({v['name']})" for v in report["violations"][:5])
        return 1, f"{detail}；既无产物又无原因的资产：{names}"
    return 0, detail


def check_art_constitution(lib: Path) -> Tuple[int, str]:
    """美术总则自洽：文件缺失＝未设定（放行），文件损坏或指纹不符＝必须失败。"""
    from .art_constitution import ArtConstitutionInvalid, CONSTITUTION_FILENAME, load_constitution

    path = Path(lib) / ".process" / CONSTITUTION_FILENAME
    if not path.is_file():
        return 0, "未建项目美术总则（回退唯一主风格包）"
    try:
        constitution = load_constitution(lib)
    except ArtConstitutionInvalid as exc:
        return 1, f"项目美术总则不可用：{exc}"
    return 0, f"项目美术总则指纹自洽（{constitution.content_fingerprint()}）"


def check_artifact_graph(lib: Path) -> Tuple[int, str]:
    """依赖图完整：有总账就必须有图，且每条边的两端都必须是图内节点。"""
    from .artifact_graph import KIND_PROMPT, ArtifactGraph
    from .asset_ledger import load_ledger

    ledger = load_ledger(lib)
    if not ledger:
        return 0, "无总账：依赖图不适用"
    try:
        graph = ArtifactGraph.load(lib)
    except ValueError as exc:
        return 1, f"依赖图不可读：{exc}"
    if not graph.nodes():
        return 1, "依赖图缺失（有总账但 .process/artifact-graph.json 为空）"

    known = {n.id for n in graph.nodes()}
    dangling: List[str] = []
    for node in graph.nodes():
        for dep in graph.dependencies(node.kind, node.key):
            if dep not in known:
                dangling.append(f"{dep} → {node.id}")
    if dangling:
        return 1, f"依赖图存在悬空边 {len(dangling)} 条：{'；'.join(sorted(dangling)[:3])}"

    prompt_nodes = {n.key for n in graph.nodes() if n.kind == KIND_PROMPT}
    required = [e.get("asset_id", "") for e in ledger
                if "base_prompt" in (e.get("required_outputs") or [])]
    absent = [aid for aid in required if aid and aid not in prompt_nodes]
    if absent:
        return 1, f"依赖图缺提示词节点 {len(absent)} 个：{'、'.join(absent[:5])}"
    return 0, f"依赖图完整（{len(known)} 节点，{len(required)} 必需提示词节点）"


def check_required_prompts(lib: Path) -> Tuple[int, str]:
    """所有必需提示词：基础提示词覆盖 + 调度清单的衍生节逐行有对应节。"""
    from .pipeline_state import _check_deriv_coverage, _check_prompt_coverage

    lib = Path(lib)
    index_path = lib / ".process" / "资产索引.json"
    if not index_path.is_file():
        return 0, "无资产索引：必需提示词不适用"
    gap = _check_prompt_coverage(lib, index_path)
    if gap:
        reason, evidence = gap
        return 1, f"基础提示词缺失（{reason}）：{evidence}"
    deriv_gap = _check_deriv_coverage(lib)
    if deriv_gap:
        reason, evidence = deriv_gap
        return 1, f"衍生提示词不齐（{reason}）：{evidence}"
    return 0, "必需提示词齐全（基础覆盖 + 衍生调度逐行对应）"


def check_visual_review(lib: Path) -> Tuple[int, str]:
    """视觉审核：只在产品结果**含生图**时参与签发。

    data_only / prompt_only 下不适用（规格 9：两线独立，视觉未启用不得拖垮资料线）；
    direct_generate 下图片审核是产品结果的一部分，未过审核即不得签发。
    """
    from .project_settings import safe_visual_mode

    mode = safe_visual_mode(Path(lib))
    if mode != "direct_generate":
        return 0, f"视觉审核不适用（产品结果为 {mode}，未选择生图）"
    plan_path = Path(lib) / "视觉资产" / "计划.toml"
    if not plan_path.is_file():
        return 1, "已选择直接生成图片，但没有视觉计划：图片计划缺失"
    try:
        from check_visual_delivery import build_visual_delivery
    except ImportError:
        from scripts.check_visual_delivery import build_visual_delivery

    result = build_visual_delivery(Path(lib), write=False)
    if result.get("status") == "DELIVERED":
        return 0, f"图片审核与视觉交付全绿（{result.get('passed')}/{result.get('tasks')}）"
    blockers = result.get("blocking") or [result.get("reason") or "视觉交付未通过"]
    return 1, f"图片审核未通过（{result.get('status')}）：{'；'.join(str(b) for b in blockers[:3])}"


def check_package_files(lib: Path) -> Tuple[int, str]:
    """包内文件：必备骨架在盘，且**当前仍然有效**的交付清单所登记产物确实在包里。

    「旧签发是否过期」不归本项判：追更后重跑 verify 时旧清单必然过期，若在这里判死，
    重新签发就永远不可能发生。时效性由 `status` / 交付门按 `_current_fingerprints` 报，
    本项只在清单仍自洽（未被新变化取代）时核对它登记的产物是否真的在包内。
    """
    from .common import file_sha256
    from .pipeline_state import _current_fingerprints

    lib = Path(lib)
    missing = [rel for rel in PACKAGE_REQUIRED if not (lib / rel).is_file()]
    if missing:
        return 1, f"交付包缺文件：{'、'.join(missing)}"

    manifest = lib / "交付清单.json"
    if not manifest.is_file():
        return 0, "尚未签发交付清单：包内文件只校验必备项"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return 1, f"交付清单不可解析：{exc}"

    current = _current_fingerprints(lib)
    stale = [key for key in ("source_fingerprint", "style_fingerprint", "asset_index_hash",
                             "artifact_hashes") if data.get(key) != current.get(key)]
    if stale:
        return 0, (f"上一份签发的 {'、'.join(stale)} 已被新变化取代：旧清单按失效处理，"
                   "本次 verify 将重新签发（时效性见 status）")

    hashes: Dict[str, Any] = data.get("artifact_hashes") or {}
    drifted = []
    for rel, want in hashes.items():
        got = file_sha256(lib / rel)
        if got is None:
            drifted.append(f"{rel}（缺失）")
        elif want and got != want:
            drifted.append(f"{rel}（内容已变）")
    if drifted:
        return 1, f"清单登记产物已失效 {len(drifted)} 个：{'；'.join(drifted[:3])}"
    return 0, f"包内文件完整（必备 {len(PACKAGE_REQUIRED)} 项 + 登记产物 {len(hashes)} 个）"


_IMPL = {
    "check_asset_coverage": check_asset_coverage,
    "check_art_constitution": check_art_constitution,
    "check_artifact_graph": check_artifact_graph,
    "check_required_prompts": check_required_prompts,
    "check_visual_review": check_visual_review,
    "check_package_files": check_package_files,
}


def run_all(lib: Path) -> List[Tuple[str, int, str]]:
    """按签发契约顺序跑全部扩展检查，返回 (名称, rc, 说明)。

    单项异常一律记为失败并带上异常类型：签发门不得因内部异常而静默放行。
    """
    results: List[Tuple[str, int, str]] = []
    for name in EXTENDED_DELIVERY_CHECKS:
        try:
            rc, detail = _IMPL[name](Path(lib))
        except Exception as exc:  # noqa: BLE001 - 检查项异常必须显式失败，不得静默通过
            rc, detail = 1, f"检查项异常：{type(exc).__name__}: {exc}"
        results.append((name, rc, detail))
    return results
