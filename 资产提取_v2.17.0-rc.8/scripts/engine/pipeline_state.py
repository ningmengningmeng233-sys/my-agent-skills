#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台流水线状态机（ADR-0084 / scripts/engine/pipeline_state.py）

由物理证据推导状态，不另建平行进度表：
- 输入：源文件指纹、段清单、登记表、段产物、反推表、资产索引、场景母版、
  描述/基础/衍生节数、校验结果、风格指纹、交付清单
- 输出：state / reason_code / evidence_path / next_command
- 规则：只看文件存在不能判完成；空/解析失败/任务失败/hash 漂移均回退或阻断
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .project_settings import safe_visual_mode
from .prompt_rules import split_names
from .skip_list import parse_skip_entries


NEXT_COMMAND: Dict[str, str] = {
    "WAITING_INPUT": "确认输入源并初始化资产库",
    "SOURCE_READY": "python scripts/split_segments.py --lib <资产库>",
    "SEGMENTED": "python scripts/scan_registry.py --lib <资产库>",
    "REGISTERED": "python scripts/extract_segments.py --lib <资产库>",
    "EXTRACTED": "python scripts/aggregate.py <段产物> <原文段> --lib <资产库>",
    "CATALOGED": "python scripts/generate_prompts.py --base <反推_最终.md> --lib <资产库>",
    "PROMPTS_READY": "python scripts/maintenance.py verify --lib <资产库> --style <画风>",
    "VERIFIED": "python scripts/maintenance.py verify --lib <资产库> --style <画风>",
    "DELIVERED": "已交付（产物与交付清单一致）",
    "BLOCKED": "检查 .process/验证报告.md 并修复失败项后重跑受影响步骤",
    "NEEDS_REVIEW": "处理待确认附录/补充画风配置后重跑 verify",
    "NEEDS_CONFIG": "配置 OpenAI 兼容接口（.env）后重跑生成步骤",
}

# R3b：交付清单必需检查集合（与 maintenance.do_verify 的签发契约一一对应）
# 工单 13：签发契约从「三张表齐全」扩展到可核验编译器事实；扩展项由
# scripts/engine/delivery_checks.py 实现，两侧名称由测试 test_asset_compiler_cli.py 钉住。
REQUIRED_DELIVERY_CHECKS = (
    "check_schedule",
    "check_final",
    "check_asset_index",
    "check_asset_coverage",
    "check_art_constitution",
    "check_artifact_graph",
    "check_required_prompts",
    "check_visual_review",
    "check_package_files",
)

DELIVERY_MANIFEST_NAME = "交付清单.json"
DELIVERY_HISTORY_DIRNAME = "交付清单历史"

# E1 签发门：启动证据不可核验的原因码（verify 拒签即取此值）
#   SOURCE_UNVERIFIABLE 启动状态文件缺失/不可解析，或原始输入不存在/未记录
#   STYLE_UNVERIFIABLE  启动状态文件未记录画风，交付画风无从核验
#   STYLE_MISMATCH      显式画风与启动状态画风不是同一张卡（别名等价不算冲突）
SOURCE_UNVERIFIABLE = "SOURCE_UNVERIFIABLE"
STYLE_UNVERIFIABLE = "STYLE_UNVERIFIABLE"
STYLE_MISMATCH = "STYLE_MISMATCH"

INIT_COMMAND = (
    "python scripts/maintenance.py init --lib <资产库> "
    "--input-source <原始txt/md> --genre <题材> --style <画风>"
)


class DeliveryRefused(ValueError):
    """签发被拒：启动证据不可核验（reason_code/detail 说明拒签原因）。

    继承 ValueError：调用方既可按 `except ValueError` 粗拦，也可读 reason_code 精确分派。
    """

    def __init__(self, reason_code: str, detail: str = ""):
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}：{detail}" if detail else reason_code)


def style_equivalent(a: str | None, b: str | None) -> bool:
    """画风标识等价判定（编号/卡名/别名）；engine.style 不可用时退化为归一化文本比较。"""
    na, nb = (a or "").strip(), (b or "").strip()
    try:
        from engine import style as style_lib
    except ImportError:
        try:
            from . import style as style_lib  # 以包方式导入（scripts.engine.pipeline_state）
        except ImportError:
            style_lib = None
    if style_lib is not None:
        return bool(style_lib.style_equivalent(na, nb))
    return na == nb


def evidence_detail(reason_code: str, state_style: str = "", expected_style: str = "") -> str:
    """把拒签原因码翻译成可直接展示的中文说明（含修复入口）。"""
    if reason_code == SOURCE_UNVERIFIABLE:
        return ("启动状态文件缺失/不可解析，或其中记录的原始输入不存在，源与产物无法核验；"
                f"确认原始输入后重跑: {INIT_COMMAND}")
    if reason_code == STYLE_UNVERIFIABLE:
        return f"启动状态文件未记录画风，交付画风无从核验；重跑: {INIT_COMMAND}"
    if reason_code == STYLE_MISMATCH:
        return (f"显式画风「{(expected_style or '').strip()}」与启动状态画风「{state_style}」不是同一张画风卡，"
                "拒绝签发（等价别名如 A2 与「仙侠奇幻风」不视为冲突）")
    return ""


def validate_start_evidence(lib: Path, expected_style: str = "") -> Tuple[bool, str, Path | None, str]:
    """校验签发前置证据：启动状态文件 + 原始输入可读 + 画风一致（E1 签发门）。

    返回 (ok, reason_code, source_path, state_style)。ok 为 False 时 reason_code 取
    SOURCE_UNVERIFIABLE / STYLE_UNVERIFIABLE / STYLE_MISMATCH 之一，ok 为 True 时取 EVIDENCE_OK。
    """
    lib = Path(lib)
    state_file = lib / ".start-state.json"
    if not state_file.is_file():
        return False, SOURCE_UNVERIFIABLE, None, ""
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, SOURCE_UNVERIFIABLE, None, ""
    if not isinstance(data, dict):
        return False, SOURCE_UNVERIFIABLE, None, ""
    raw = str(data.get("input_source") or "").strip()
    if not raw:
        return False, SOURCE_UNVERIFIABLE, None, ""
    src = Path(raw)
    if not src.is_absolute():
        src = lib / src
    if not src.is_file():
        return False, SOURCE_UNVERIFIABLE, src, ""
    state_style = str(data.get("style") or "").strip()
    if not state_style:
        return False, STYLE_UNVERIFIABLE, src, ""
    # 规范化项目（规格 4）：全部原始源与顺序清单必须仍在原位，否则源证据不可核验
    _fp, manifest_missing = _source_manifest_state(lib, data)
    if manifest_missing:
        return False, SOURCE_UNVERIFIABLE, src, ""
    want = (expected_style or "").strip()
    if want and not style_equivalent(want, state_style):
        return False, STYLE_MISMATCH, src, state_style
    return True, "EVIDENCE_OK", src, state_style


def _source_manifest_state(lib: Path, state: dict) -> Tuple[str, bool]:
    """规范化项目的原始来源状态（规格 4）。返回 (指纹片段, 是否有源缺失)。

    指纹覆盖：全部原文件哈希、顺序清单哈希、规范化原文哈希与来源清单哈希。
    任一原始源变化或缺失都会改变片段，从而让交付清单立即失效（不再 DELIVERED）。
    """
    raw_manifest = str(state.get("source_manifest_path") or "").strip()
    if not raw_manifest:
        return "", False
    manifest_path = Path(raw_manifest)
    if not manifest_path.is_absolute():
        manifest_path = lib / manifest_path
    missing = False
    parts: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "<source-manifest-unreadable>", True
    if not isinstance(manifest, dict):
        return "<source-manifest-malformed>", True
    parts.append(f"manifest:{file_fingerprint(manifest_path) or '<unreadable>'}")
    for meta in manifest.get("sources", []):
        if not isinstance(meta, dict):
            continue
        src = Path(str(meta.get("source_path") or ""))
        fp = file_fingerprint(src) if src.is_file() else None
        if fp is None:
            missing = True
            fp = "<missing>"
        parts.append(f"{meta.get('name')}:{fp}")
    order_raw = str(state.get("source_order_file") or manifest.get("order_file") or "").strip()
    if order_raw:
        order_path = Path(order_raw)
        if not order_path.is_absolute():
            order_path = lib / order_path
        parts.append(f"order:{file_fingerprint(order_path) or '<missing>'}")
        if not order_path.is_file():
            missing = True
    normalized_raw = str(state.get("input_source") or "").strip()
    if normalized_raw:
        np = Path(normalized_raw)
        if not np.is_absolute():
            np = lib / np
        parts.append(f"normalized:{file_fingerprint(np) or '<missing>'}")
    return "|".join(parts), missing


def file_fingerprint(path: Path) -> str | None:
    from .common import file_sha256

    return file_sha256(path)


def dir_fingerprint(paths: List[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        if p.is_file():
            try:
                h.update(p.read_bytes())
            except OSError:
                h.update(b"<unreadable>")
    return h.hexdigest()[:16]


def _non_empty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def derive_pipeline_state(
    lib: Path,
    verify_results: List[Tuple[str, int]] | None = None,
    style: str | None = None,
    has_config: bool = True,
) -> Dict[str, Any]:
    """推导流水线状态（只读）。verify_results 为 maintenance.verify 的检查结果。"""
    lib = Path(lib)
    evidence: List[str] = []

    # 空库 / 无输入
    seg_dirs = [dd for dd in (lib / "原文段").glob("*") if dd.is_dir()] if (lib / "原文段").is_dir() else []
    segments = [sf for dd in seg_dirs for sf in dd.glob("*.md") if sf.is_file()]
    registries = list(lib.glob("*-登记表.md"))
    seg_products = [pd for pd in (lib / "段产物").glob("*") if pd.is_dir()] if (lib / "段产物").is_dir() else []
    seg_product_files = [pf for pd in seg_products for pf in pd.glob("*.md")] if seg_products else []
    final = lib / "反推_最终.md"
    index_path = lib / ".process" / "资产索引.json"
    master_path = lib / "连续性" / "场景母版.md"
    delivery_path = lib / "交付清单.json"

    if not segments and not registries and not seg_product_files and not final.is_file():
        return {
            "state": "WAITING_INPUT",
            "reason_code": "EMPTY_LIB",
            "evidence_path": str(lib),
            "next_command": NEXT_COMMAND["WAITING_INPUT"],
        }

    # 源就绪
    if not segments:
        return {
            "state": "SOURCE_READY" if has_config else "NEEDS_CONFIG",
            "reason_code": "NO_SEGMENTS",
            "evidence_path": str(lib / "原文段"),
            "next_command": NEXT_COMMAND["NEEDS_CONFIG" if not has_config else "SOURCE_READY"],
        }

    # SEGMENTED：段非空 + 无过期段 + 无失败任务
    empty_segs = [str(p) for p in segments if p.stat().st_size == 0]
    if empty_segs:
        return {
            "state": "BLOCKED",
            "reason_code": "EMPTY_SEGMENT",
            "evidence_path": empty_segs[0],
            "next_command": NEXT_COMMAND["BLOCKED"],
        }
    stale = _find_stale_segments(lib, segments)
    if stale:
        return {
            "state": "BLOCKED",
            "reason_code": "STALE_SEGMENT",
            "evidence_path": stale[0],
            "next_command": NEXT_COMMAND["BLOCKED"],
        }
    task_err = _check_task_tables(lib)
    if task_err:
        reason, ev = task_err
        return {
            "state": "BLOCKED",
            "reason_code": reason,
            "evidence_path": ev,
            "next_command": NEXT_COMMAND["BLOCKED"],
        }

    if not registries:
        return {
            "state": "SEGMENTED",
            "reason_code": "NO_REGISTRY",
            "evidence_path": str(lib),
            "next_command": NEXT_COMMAND["SEGMENTED"],
        }

    # REGISTERED：登记表存在 + 段产物覆盖全部段（数量与任务一致）
    seg_names = {p.name for p in segments}
    prod_names = {p.name.replace("-资产记录.md", ".md") for p in seg_product_files} if seg_product_files else set()
    if not seg_product_files or not seg_names <= prod_names:
        missing = sorted(seg_names - prod_names)
        return {
            "state": "REGISTERED",
            "reason_code": "SEG_PRODUCTS_INCOMPLETE",
            "evidence_path": missing[0] if missing else str(lib / "段产物"),
            "next_command": NEXT_COMMAND["REGISTERED"],
        }

    # EXTRACTED：段产物非空可解析
    for seg_file in seg_product_files:
        try:
            if seg_file.stat().st_size == 0:
                return {
                    "state": "BLOCKED",
                    "reason_code": "EMPTY_SEG_PRODUCT",
                    "evidence_path": str(seg_file),
                    "next_command": NEXT_COMMAND["BLOCKED"],
                }
        except OSError:
            return {
                "state": "BLOCKED",
                "reason_code": "UNREADABLE_SEG_PRODUCT",
                "evidence_path": str(seg_file),
                "next_command": NEXT_COMMAND["BLOCKED"],
            }

    if not _non_empty(final):
        return {
            "state": "EXTRACTED",
            "reason_code": "NO_FINAL",
            "evidence_path": str(final),
            "next_command": NEXT_COMMAND["EXTRACTED"],
        }

    # CATALOGED：四表 + 索引 + 母版可解析；索引 ID 必须覆盖四表 ID
    index_ok, index_reason = _check_index(index_path)
    if not index_ok:
        return {
            "state": "EXTRACTED",
            "reason_code": index_reason,
            "evidence_path": str(index_path),
            "next_command": NEXT_COMMAND["EXTRACTED"],
        }
    table_gap = _check_final_index_coverage(final, index_path)
    if table_gap:
        return {
            "state": "EXTRACTED",
            "reason_code": "FINAL_INDEX_MISMATCH",
            "evidence_path": table_gap,
            "next_command": NEXT_COMMAND["EXTRACTED"],
        }
    if not _non_empty(master_path):
        # 母版缺失不阻断编目（老库兼容），但记录为待办；仍视为 CATALOGED
        evidence.append("母版缺失（老库兼容）")

    # PROMPTS_READY：P0-P2 资产必须有基础提示词节；P3 以跳过清单为合法排除证据
    prompt_gap = _check_prompt_coverage(lib, index_path)
    if prompt_gap:
        reason, ev = prompt_gap
        if reason == "EMPTY_PROMPT_DOC":
            return {
                "state": "BLOCKED",
                "reason_code": reason,
                "evidence_path": ev,
                "next_command": NEXT_COMMAND["BLOCKED"],
            }
        return {
            "state": "CATALOGED",
            "reason_code": reason,
            "evidence_path": ev,
            "next_command": NEXT_COMMAND["CATALOGED"],
        }
    # 衍生覆盖：调度清单行必须在衍生文档中有对应节（残留旧节同样阻断）
    deriv_gap = _check_deriv_coverage(lib)
    if deriv_gap:
        reason, ev = deriv_gap
        return {
            "state": "BLOCKED" if reason == "STALE_DERIV_SECTION" else "PROMPTS_READY",
            "reason_code": reason,
            "evidence_path": ev,
            "next_command": NEXT_COMMAND["BLOCKED" if reason == "STALE_DERIV_SECTION" else "PROMPTS_READY"],
        }
    pending_conflicts = _count_pending_conflicts(index_path)
    if pending_conflicts > 0:
        return {
            "state": "NEEDS_REVIEW",
            "reason_code": "PENDING_CONFLICTS",
            "evidence_path": str(index_path),
            "next_command": NEXT_COMMAND["NEEDS_REVIEW"],
        }

    # VERIFIED / DELIVERED：先看已签发清单（文件证据），再看即时验证结果
    # 语义：DELIVERED = 产物完整 + 清单当前有效；VERIFIED = 验证通过但清单缺失/过期；
    # PROMPTS_READY = 产物完整但尚未验证。绝不用“上次成功”替代当前文件证据。
    # 签发前置证据：画风冲突、或清单存在但启动证据不可核验时，一律显式降级为 NEEDS_REVIEW，
    # 不得伪装 DELIVERED/VERIFIED（E1 签发门；无清单的库保持既有 PROMPTS_READY 语义）
    evidence_ok, evidence_reason, _ev_src, _ev_style = validate_start_evidence(lib, (style or "").strip())
    delivery = _check_delivery(lib, delivery_path)
    if evidence_reason == STYLE_MISMATCH or (delivery_path.is_file() and not evidence_ok):
        return {
            "state": "NEEDS_REVIEW",
            "reason_code": evidence_reason,
            "evidence_path": str(lib / ".start-state.json"),
            "next_command": NEXT_COMMAND["NEEDS_REVIEW"],
        }
    if delivery["valid"]:
        return {
            "state": "DELIVERED",
            "reason_code": "DELIVERY_VALID",
            "evidence_path": str(delivery_path),
            "next_command": NEXT_COMMAND["DELIVERED"],
        }
    if verify_results is not None:
        if any(rc != 0 for _, rc in verify_results):
            return {
                "state": "BLOCKED",
                "reason_code": "VERIFY_FAILED",
                "evidence_path": str(lib / ".process" / "验证报告.md"),
                "next_command": NEXT_COMMAND["BLOCKED"],
            }
        if not style:
            return {
                "state": "NEEDS_REVIEW",
                "reason_code": "VERIFY_NO_STYLE",
                "evidence_path": str(lib),
                "next_command": NEXT_COMMAND["NEEDS_REVIEW"],
            }
        # 即时验证全绿但清单无效 → VERIFIED（等待签发），原因透出清单失效点
        return {
            "state": "VERIFIED",
            "reason_code": delivery["reason"],
            "evidence_path": str(delivery_path),
            "next_command": NEXT_COMMAND["VERIFIED"],
        }
    # 无即时验证结果：清单无效时区分“从未验证”与“清单过期”
    if delivery["reason"] == "NO_DELIVERY_MANIFEST":
        return {
            "state": "PROMPTS_READY",
            "reason_code": "AWAITING_VERIFY",
            "evidence_path": str(lib / "提示词"),
            "next_command": NEXT_COMMAND["PROMPTS_READY"],
        }
    return {
        "state": "VERIFIED",
        "reason_code": delivery["reason"],
        "evidence_path": str(delivery_path),
        "next_command": NEXT_COMMAND["VERIFIED"],
    }


# ---------------------------------------------------------------------------
# 双交付状态线（规格 9）：资产资料交付与视觉资产交付互不阻塞
# ---------------------------------------------------------------------------

VISUAL_OPTION_NOT_ENABLED = "VISUAL_OPTION_NOT_ENABLED"
VISUAL_PLAN_CREATED = "VISUAL_PLAN_CREATED"
VISUAL_PLAN_REL = ("视觉资产", "计划.toml")
VISUAL_DELIVERY_MANIFEST = "视觉交付清单.json"

# 视觉线的推进阶段（工单 13）：图片计划 → 生成 → 审核 → 返工 → 签发
VISUAL_STAGES = ("PLANNED", "GENERATING", "REVIEWING", "REWORK", "SIGNED")
VISUAL_STAGE_NONE = "NOT_PLANNED"

_STAGE_BY_STATUS = {
    "PLANNED": "PLANNED",
    "GENERATED": "GENERATING",
    "QC_PENDING": "GENERATING",
    "AWAITING_MANUAL_RESULT": "GENERATING",
    "AWAITING_VISUAL_REVIEW": "GENERATING",
    "WAITING_REVIEW": "REVIEWING",
    "NEEDS_REVIEW": "REVIEWING",
    "NEEDS_HUMAN_DECISION": "REVIEWING",
    "NEEDS_REVISION": "REWORK",
    "FAILED": "REWORK",
    "BLOCKED": "REWORK",
    "ACCEPTED": "SIGNED",
    "READY_AS_REFERENCE": "SIGNED",
}
# 汇报优先级：返工与审核必须先于「生成中」被看到；SIGNED 只在全部任务已通过时成立
_STAGE_PRIORITY = ("REWORK", "REVIEWING", "GENERATING", "PLANNED", "SIGNED")


def visual_stage(counts: Dict[str, int]) -> str:
    """由任务状态计数推导当前阶段（只读派生，不新增进度表）。"""
    if not counts:
        return VISUAL_STAGE_NONE
    present = {_STAGE_BY_STATUS.get(status) for status in counts if counts.get(status)}
    present.discard(None)
    if not present:
        return VISUAL_STAGE_NONE
    for stage in _STAGE_PRIORITY:
        if stage in present:
            return stage
    return VISUAL_STAGE_NONE


def _latest_failed_tasks(lib: Path) -> Dict[str, str]:
    """写前日志里**最新一条**事件为失败的任务 → 事件类型。

    事件日志是写前日志：图片已失败但计划尚未写入时，计划仍显示旧状态。
    因此「最新失败」必须由日志本身判定，而不是等计划更新。
    """
    try:
        from .visual_events import TASK_BLOCKED, TRANSPORT_FAILED, read_events
    except ImportError:
        return {}
    latest: Dict[str, str] = {}
    for event in read_events(lib):
        if event.task_id:
            latest[event.task_id] = event.kind
    return {task_id: kind for task_id, kind in latest.items()
            if kind in (TRANSPORT_FAILED, TASK_BLOCKED)}


def visual_delivery_state(lib: Path) -> Dict[str, Any]:
    """可选视觉交付线（规格 9）+ 项目设置的视觉模式（阶段 1 工单 1.1）。

    模式来自 `.process/项目设置.toml`，安全读取：设置损坏时退回 `data_only`，
    核心资料线不因设置问题中断。
    """
    state = _visual_delivery_state(Path(lib))
    state["mode"] = safe_visual_mode(Path(lib))
    return state


def _visual_delivery_state(lib: Path) -> Dict[str, Any]:
    """可选视觉交付线（规格 9）：未启用是**合法状态**，但不是「视觉交付通过」。

    未创建视觉计划 → NOT_ENABLED（reason_code=VISUAL_NOT_ENABLED，退出码非零）；
    有计划但未全通过 → PLANNED/PARTIAL；权威包缺失或产物失效 → BLOCKED 并给出原因；
    全部通过且已签发视觉交付清单 → DELIVERED。核心资料签发在任何情况下都不读取本函数结果。
    """
    lib = Path(lib)
    plan_path = lib / VISUAL_PLAN_REL[0] / VISUAL_PLAN_REL[1]
    if not plan_path.is_file():
        return {"state": "NOT_ENABLED", "reason_code": VISUAL_OPTION_NOT_ENABLED,
                "stage": VISUAL_STAGE_NONE,
                "reason": "未创建视觉计划（视觉未启用是合法状态，但不等于视觉交付通过）"}
    try:  # 延迟导入：核心状态机不静态依赖视觉模块
        from .visual_schema import load_visual_plan
    except ImportError:
        return {"state": "PLANNED", "reason_code": VISUAL_PLAN_CREATED,
                "stage": VISUAL_STAGE_NONE}
    try:
        plan = load_visual_plan(plan_path)
    except (OSError, ValueError) as e:
        return {"state": "BLOCKED", "reason_code": "VISUAL_PLAN_UNREADABLE",
                "stage": VISUAL_STAGE_NONE, "reason": f"视觉计划不可解析：{e}"}

    counts = plan.counts_by_status()
    total = len(plan.tasks)
    passed = counts.get("ACCEPTED", 0) + counts.get("READY_AS_REFERENCE", 0)
    stage = visual_stage(counts)
    base = {"counts": counts, "total": total, "passed": passed, "stage": stage}

    # 最新失败优先于一切「计划看起来还行」的推断：写前日志说了失败就是失败。
    # 阶段也一并归到 REWORK：否则会出现「状态 BLOCKED、阶段 SIGNED」这种自相矛盾的报告。
    failed = _latest_failed_tasks(lib)
    if failed:
        names = "、".join(sorted(failed)[:3])
        return {"state": "BLOCKED", "reason_code": "VISUAL_LATEST_FAILURE",
                "reason": f"{names} 最新一次生成失败（{failed[sorted(failed)[0]]}）："
                          "最新失败立即撤销实时交付，需返工或重跑后重新审核",
                **{**base, "stage": "REWORK"}}

    # 风格包缺失/漂移、已通过产物无法核验：属阻断，不得用 NOT_ENABLED 逃逸
    blocked = _visual_blocking_reasons(lib, plan)
    if blocked:
        return {"state": "BLOCKED", "reason_code": "VISUAL_BLOCKED",
                "reason": "；".join(blocked[:3]), **base}

    if total == 0:
        return {"state": "PLANNED", "reason_code": VISUAL_PLAN_CREATED, **base}
    if passed == total:
        manifest = lib / VISUAL_PLAN_REL[0] / VISUAL_DELIVERY_MANIFEST
        if manifest.is_file():
            return {"state": "DELIVERED", "reason_code": "VISUAL_DELIVERY_VALID", **base}
        return {"state": "PARTIAL", "reason_code": "VISUAL_DELIVERY_NOT_SIGNED", **base}
    if passed:
        return {"state": "PARTIAL", "reason_code": "VISUAL_PARTIAL", **base}
    return {"state": "PLANNED", "reason_code": VISUAL_PLAN_CREATED, **base}


def _visual_blocking_reasons(lib: Path, plan) -> List[str]:
    """视觉线阻断原因（权威包缺失/漂移、已通过产物无法核验）。异常即忽略，不影响资料线。"""
    out: List[str] = []
    try:
        from .style_registry import pack_root, resolve_style_record
        from .visual_request_chain import stylepack_integrity_issues, unverifiable_accepted

        record = resolve_style_record(plan.style_slug)
        if record is not None:
            if record.pack_hash and plan.style_hash and record.pack_hash != plan.style_hash:
                out.append("风格包内容哈希与登记表不一致（权威包已变化，需显式升级项目）")
            root = Path(pack_root(record.pack_slug, record.pack_version))
            if not root.is_dir():
                out.append(f"权威风格包不存在：{record.pack_slug}@{record.pack_version}")
            else:
                out.extend(stylepack_integrity_issues(root, require_images=True))
        for task_id, code, detail in unverifiable_accepted(plan, lib):
            out.append(f"{task_id} 产物无法核验（{code}：{detail}）")
    except Exception:  # noqa: BLE001 - 状态查询绝不因视觉模块异常而中断
        return out
    return out


def collect_pipeline_state(
    lib: Path,
    verify_results: List[Tuple[str, int]] | None = None,
    style: str | None = None,
    has_config: bool = True,
) -> Dict[str, Any]:
    """核心状态 + 双交付状态线（规格 9）。

    顶层 state/reason_code/evidence_path/next_command 与 derive_pipeline_state 逐字一致，
    旧调用者不受影响；data_delivery 镜像核心线，visual_delivery 独立成线。
    视觉模块不可用、未启用或失败都不改变 data_delivery 的判定（规格 3.1/3.2）。
    """
    lib = Path(lib)
    result: Dict[str, Any] = dict(
        derive_pipeline_state(lib, verify_results=verify_results, style=style, has_config=has_config)
    )
    result["data_delivery"] = {"state": result["state"], "reason_code": result["reason_code"]}
    result["visual_delivery"] = visual_delivery_state(lib)
    result["compiler_stages"] = compiler_stages(lib, result["visual_delivery"])
    return result


STAGE_DONE, STAGE_PENDING, STAGE_BLOCKED, STAGE_NA = "DONE", "PENDING", "BLOCKED", "NOT_APPLICABLE"


def compiler_stages(lib: Path, visual: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """链条各阶段的**事实快照**（只读派生，不另建进度表）。

    每一项都给出证据路径或计数，`status` 据此报告当前位置；未启用的阶段一律
    NOT_APPLICABLE 而不是伪装成失败（规格 9）。
    """
    lib = Path(lib)
    facts = lib / ".process" / "source-facts.jsonl"
    ledger_path = lib / ".process" / "asset-ledger.json"
    base_dir = lib / "提示词" / "基础"
    out: Dict[str, Any] = {}

    def stage(state: str, detail: str, evidence: str = "") -> Dict[str, str]:
        return {"state": state, "detail": detail, "evidence": evidence}

    facts_n = 0
    if facts.is_file():
        try:
            facts_n = sum(1 for line in facts.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            facts_n = 0
    out["事实库"] = stage(STAGE_DONE if facts_n else STAGE_PENDING,
                       f"{facts_n} 条源事实", str(facts))

    ledger = []
    try:
        from .asset_ledger import load_ledger
        ledger = load_ledger(lib)
    except (ImportError, ValueError):
        ledger = []
    out["总账"] = stage(STAGE_DONE if ledger else STAGE_PENDING,
                      f"{len(ledger)} 条总账记录", str(ledger_path))

    prompted = 0
    if base_dir.is_dir():
        for p in base_dir.glob("*.md"):
            text = p.read_text(encoding="utf-8", errors="replace")
            prompted += len(re.findall(r"^## \d{3}\. ", text, re.M))
    out["提示词"] = stage(STAGE_DONE if prompted else STAGE_PENDING,
                       f"{prompted} 节基础提示词", str(base_dir))

    visual = visual if visual is not None else visual_delivery_state(lib)
    counts = visual.get("counts") or {}
    if visual.get("state") == "NOT_ENABLED":
        for name in ("图片计划", "生成", "审核", "返工"):
            out[name] = stage(STAGE_NA, "视觉未启用（合法状态）")
    else:
        # 「待生成」= 还没出图的任务；「已出图」= 已有产物的任务（含已通过与待返工）
        planned_states = ("PLANNED", "GENERATED", "QC_PENDING", "AWAITING_MANUAL_RESULT",
                          "AWAITING_VISUAL_REVIEW", "WAITING_REVIEW", "NEEDS_REVIEW",
                          "NEEDS_HUMAN_DECISION", "NEEDS_REVISION", "FAILED", "BLOCKED")
        generated_states = tuple(s for s in planned_states if s != "PLANNED") + (
            "ACCEPTED", "READY_AS_REFERENCE")
        planned = sum(counts.get(k, 0) for k in planned_states)
        generated = sum(counts.get(k, 0) for k in generated_states)
        reviewed = counts.get("ACCEPTED", 0) + counts.get("READY_AS_REFERENCE", 0)
        rework = sum(counts.get(k, 0) for k in ("NEEDS_REVISION", "FAILED", "BLOCKED"))
        total = int(visual.get("total") or 0)
        out["图片计划"] = stage(STAGE_DONE if total else STAGE_PENDING,
                            f"{total} 项计划任务（待生成 {planned}）",
                            str(lib.joinpath(*VISUAL_PLAN_REL)))
        out["生成"] = stage(STAGE_DONE if total and generated == total else STAGE_PENDING,
                          f"已出图 {generated}/{total}")
        out["审核"] = stage(STAGE_DONE if total and reviewed == total else STAGE_PENDING,
                          f"已通过审核 {reviewed}/{total}")
        out["返工"] = stage(STAGE_BLOCKED if rework else STAGE_DONE,
                          f"{rework} 项待返工" if rework else "无待返工项")

    signed = visual.get("state") == "DELIVERED"
    out["签发"] = stage(STAGE_DONE if signed else STAGE_PENDING,
                      "视觉交付清单有效" if signed else "视觉线未签发（资料线签发见 data_delivery）",
                      str(lib / VISUAL_PLAN_REL[0] / VISUAL_DELIVERY_MANIFEST))
    return out


def _check_index(index_path: Path) -> Tuple[bool, str]:
    if not index_path.is_file():
        return False, "NO_ASSET_INDEX"
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "ASSET_INDEX_UNPARSABLE"
    if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
        return False, "ASSET_INDEX_MALFORMED"
    # 结构化校验：关系端点存在性（悬空边即未完成编目）
    ids = {a.get("asset_id", "") for a in data["assets"] if isinstance(a, dict)}
    for a in data["assets"]:
        if not isinstance(a, dict):
            return False, "ASSET_INDEX_MALFORMED"
        for rel in (a.get("relations", []) or []):
            if isinstance(rel, dict) and rel.get("target") not in ids:
                return False, "ASSET_INDEX_DANGLING_RELATION"
    return True, "INDEX_OK"


def _read_db_rows(lib: Path, table: str) -> List[Dict[str, Any]]:
    """只读断点库任务行；库缺失/表缺失返回空（由文件证据兜底）。"""
    import sqlite3

    db_path = lib / ".asset_state.db"
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(db_path)
        try:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "segment_path" not in cols or "status" not in cols:
                return []
            rows = conn.execute(f"SELECT segment_path, status, error FROM {table}").fetchall()
            return [{"segment_path": r[0], "status": r[1], "error": r[2] if len(r) > 2 else ""} for r in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _find_stale_segments(lib: Path, segments: List[Path]) -> List[str]:
    """过期段：断点库有记录但文件已不存在（改名/范围变化残留）。"""
    names = {p.name for p in segments}
    stale: List[str] = []
    for table in ("segment_tasks", "registry_tasks"):
        for row in _read_db_rows(lib, table):
            base = Path(str(row.get("segment_path", ""))).name
            if base and base.endswith(".md") and base not in names and "资产记录" not in base:
                stale.append(f"{table}:{base}")
    return sorted(set(stale))


def _check_task_tables(lib: Path) -> Tuple[str, str] | None:
    """失败任务阻断：任一任务表有 failed 即 BLOCKED。"""
    for table in ("segment_tasks", "registry_tasks"):
        for row in _read_db_rows(lib, table):
            if str(row.get("status", "")) == "failed":
                return ("TASK_FAILED", str(row.get("segment_path", "") or table))
    return None


def _parse_final_asset_ids(final: Path) -> List[str]:
    """解析反推四表中的旧展示 ID（CH_/SC_/PR_/CO_）。"""
    ids: List[str] = []
    try:
        text = final.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and re.match(r"^(CH|SC|PR|CO)_", cells[0]):
            ids.append(cells[0])
    return ids


def _check_final_index_coverage(final: Path, index_path: Path) -> str:
    """反推四表 ID 必须全部在索引中；缺口返回首个缺失 ID，否则空串。"""
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return str(index_path)
    ids = {a.get("asset_id", "") for a in data.get("assets", []) if isinstance(a, dict)}
    for aid in _parse_final_asset_ids(final):
        if aid not in ids:
            return aid
    return ""


def _parse_merged_names(path: Path) -> List[str]:
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    if not text.strip():
        return ["<empty>"]
    return re.findall(r"^## \d{3}\. (.+)$", text, re.M)


def _check_prompt_coverage(lib: Path, index_path: Path) -> Tuple[str, str] | None:
    """P0-P2 资产必须有基础提示词节；P3 以跳过清单为合法排除证据。

    返回 None 表示覆盖完整；否则 (reason, evidence)。
    """
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ("ASSET_INDEX_UNPARSABLE", str(index_path))
    skip_ids: set[str] = set()
    skip_path = lib / "提示词" / "基础" / "跳过清单.md"
    if skip_path.is_file():
        try:
            skip_ids = set(parse_skip_entries(skip_path.read_text(encoding="utf-8")))
        except OSError:
            skip_ids = set()
    prefix_unused = {"人物": "CH_", "场景": "SC_", "道具": "PR_"}
    _ = prefix_unused
    sections: Dict[str, List[str]] = {}
    empty_doc = ""
    for t in ("人物", "场景", "道具"):
        p = lib / "提示词" / "基础" / f"{t}提示词.md"
        names = _parse_merged_names(p)
        if names == ["<empty>"]:
            empty_doc = str(p)
        sections[t] = names
    if empty_doc:
        return ("EMPTY_PROMPT_DOC", empty_doc)
    for a in data.get("assets", []):
        if not isinstance(a, dict):
            continue
        if a.get("type") not in ("人物", "场景", "道具"):
            continue
        if a.get("priority") == "P3":
            continue
        if a.get("fact_level") == "待定冲突":
            continue
        aid = a.get("asset_id", "")
        name = a.get("name", "")
        t = a.get("type", "")
        # 精确匹配节标题（主名 + 索引登记的别名）；**不得**用子串：
        # `name in s or s in name` 下，索引里的「李寻」会被「李寻欢」的节判成已覆盖（假放行），
        # 「侍卫」也会被「侍卫甲」放行。别名口径与 check_schedule.load_index_aliases 同源（alias_display）。
        wanted = {name} if name else set()
        wanted |= split_names(str(a.get("alias_display") or ""))
        hit = bool(wanted & set(sections.get(t, [])))
        if not hit:
            # 跳过清单明确记录才算合法排除（**精确 ID**：子串匹配下 `CH_侍卫` 会被
            # 清单里的 `CH_侍卫甲` 放行，等于假阴性；解析与另两条门同源）
            if aid and aid in skip_ids:
                continue
            return ("BASE_PROMPT_MISSING", f"{aid or name}")
    if not any(sections.values()):
        return ("NO_BASE_PROMPTS", str(lib / "提示词" / "基础"))
    return None


def _parse_schedule_items(lib: Path) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    p = lib / "提示词" / "调度清单.md"
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| 类型"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 3 and cells[0] in ("人物", "场景", "道具"):
            items.append({"类型": cells[0], "资产": cells[1], "变体": cells[2]})
    return items


def _check_deriv_coverage(lib: Path) -> Tuple[str, str] | None:
    """调度清单行必须在衍生文档中有节；衍生物料残留旧节同样阻断。"""
    items = _parse_schedule_items(lib)
    if not items:
        return None
    from .common import load_asset_types
    from .prompt_rules import DERIV_CHANNEL_TYPES
    type_by_name = load_asset_types(lib)
    sections: Dict[str, List[str]] = {}
    for t in ("人物", "场景", "道具"):
        sections[t] = _parse_merged_names(lib / "提示词" / "衍生" / f"{t}提示词.md")
    for it in items:
        t, asset = it["类型"], it["资产"]
        # 类型以**资产索引**为准：服装没有衍生通道（走服饰设计线），生成侧已记名略过，
        # 这里照旧索要就会把失败从生成步骤搬到 verify。
        if (type_by_name.get(asset, "") or t) not in DERIV_CHANNEL_TYPES:
            continue
        if not any(asset and (asset in s or s in asset) for s in sections.get(t, [])):
            return ("DERIV_PROMPT_MISSING", f"{t}「{asset}」变体{it['变体']}")
    # 残留旧节：衍生节对应的资产已不在调度清单且不在索引 → 阻断清理
    wanted = {(it["类型"], it["资产"]) for it in items}
    for t, names in sections.items():
        for s in names:
            if s and not any(asset in s or s in asset for _, asset in wanted):
                return ("STALE_DERIV_SECTION", f"{t}#{s}")
    return None


def _count_pending_conflicts(index_path: Path) -> int:
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    n = 0
    for a in data.get("assets", []):
        if isinstance(a, dict) and a.get("fact_level") == "待定冲突":
            n += 1
    return n


def _check_delivery(lib: Path, delivery_path: Path) -> Dict[str, Any]:
    if not delivery_path.is_file():
        return {"valid": False, "reason": "NO_DELIVERY_MANIFEST"}
    # 清单存在但启动证据不可核验时，任何自洽的指纹都不足以判交付（E1 签发门）
    evidence_ok, evidence_reason, _src, state_style = validate_start_evidence(lib)
    if not evidence_ok:
        return {"valid": False, "reason": evidence_reason}
    try:
        data = json.loads(delivery_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"valid": False, "reason": "DELIVERY_UNPARSABLE"}
    # 清单本身必须是有效签发：状态 DELIVERED + 全部检查 rc=0 + 必需检查集合齐全
    if data.get("status") != "DELIVERED":
        return {"valid": False, "reason": "DELIVERY_NOT_DELIVERED"}
    checks = data.get("checks", [])
    if not isinstance(checks, list) or not checks or any(
        not isinstance(c, dict) or c.get("rc", 1) != 0 for c in checks
    ):
        return {"valid": False, "reason": "DELIVERY_CHECKS_FAILED"}
    # R3b：必需检查集合以 maintenance.verify 的签发契约为唯一来源；
    # 只写单个检查的清单不得冒充完整签发（服饰检查按实际触发附加，不计入必需集）
    names = {c.get("name") for c in checks if isinstance(c, dict)}
    if any(n not in names for n in REQUIRED_DELIVERY_CHECKS):
        return {"valid": False, "reason": "DELIVERY_CHECKS_INCOMPLETE"}
    current = _current_fingerprints(lib)
    for key in ("source_fingerprint", "style_fingerprint", "asset_index_hash", "artifact_hashes"):
        if key not in data:
            return {"valid": False, "reason": "DELIVERY_INCOMPLETE"}
    if data.get("source_fingerprint") != current["source_fingerprint"]:
        return {"valid": False, "reason": "SOURCE_CHANGED"}
    if data.get("style_fingerprint") != current["style_fingerprint"]:
        return {"valid": False, "reason": "STYLE_CHANGED"}
    if data.get("asset_index_hash") != current["asset_index_hash"]:
        return {"valid": False, "reason": "INDEX_CHANGED"}
    if data.get("artifact_hashes") != current["artifact_hashes"]:
        return {"valid": False, "reason": "ARTIFACTS_CHANGED"}
    # 指纹已被改启动画风的情形判为 STYLE_CHANGED（更有指向性）；指纹自洽但清单
    # 自称画风与启动状态不同卡时，只剩这一道能拦「状态 H12、清单 A2」的伪签发。
    manifest_style = str(data.get("style") or "").strip()
    if manifest_style and not style_equivalent(manifest_style, state_style):
        return {"valid": False, "reason": STYLE_MISMATCH}
    return {"valid": True, "reason": "DELIVERY_VALID"}


def _style_content_fingerprint(style_name: str) -> str:
    """画风内容指纹：名称 + 画风卡分层/定性 + 风格包文件内容。

    只对名称哈希会漏判“卡片内容被改但名称不变”；行级画风覆盖由调度清单
    哈希覆盖（调度清单纳入产物指纹）。
    """
    h = hashlib.sha256()
    h.update((style_name or "").strip().encode("utf-8"))
    try:
        from engine import style as style_lib
        card, mode = style_lib.resolve_style(None, (style_name or "").strip())
        if mode == "card" and card:
            h.update(str(card.get("zh", "")).encode("utf-8"))
            for layer in (card.get("layers") or []):
                h.update(str(layer).encode("utf-8"))
            h.update(str(card.get("negative", "")).encode("utf-8"))
            pack_id = str(card.get("pack", "") or "").strip()
            if pack_id:
                pack_dir = Path(style_lib.STYLE_PACK_DIR) / pack_id
                if pack_dir.is_dir():
                    for p in sorted(pack_dir.rglob("*.md")):
                        if p.is_file():
                            try:
                                h.update(p.read_bytes())
                            except OSError:
                                h.update(b"<unreadable>")
    except Exception:
        h.update(b"<style-unresolved>")
    return h.hexdigest()[:16]


def _current_fingerprints(lib: Path) -> Dict[str, Any]:
    # 源指纹：原始输入文件（.start-state.json 的 input_source）+ 段清单/段内容。
    # 仅对 原文段/*.md 求哈希无法发现“源已变、尚未重拆段”；缺失输入路径标 NEEDS_REVIEW 由上层处理。
    seg_files: List[Path] = []
    seg_root = lib / "原文段"
    if seg_root.is_dir():
        seg_files = sorted([p for p in seg_root.rglob("*.md") if p.is_file()])
    input_source_fp = ""
    input_source_missing = False
    state_path = lib / ".start-state.json"
    style_name = ""
    manifest_fp = ""
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            style_name = (state.get("style") or "").strip()
            raw_input = (state.get("input_source") or "").strip()
            if raw_input:
                ipath = Path(raw_input)
                if not ipath.is_absolute():
                    ipath = lib / ipath
                if ipath.is_file():
                    input_source_fp = file_fingerprint(ipath) or ""
                else:
                    input_source_missing = True
            else:
                input_source_missing = True
            # 规范化项目：原始源/顺序清单/规范化文件哈希一并纳入源指纹（规格 4）
            manifest_fp, manifest_missing = _source_manifest_state(lib, state)
            if manifest_missing:
                input_source_missing = True
        except (OSError, ValueError):
            input_source_missing = True
    else:
        input_source_missing = True
    h = hashlib.sha256()
    h.update(dir_fingerprint(seg_files).encode("utf-8"))
    h.update(b"|")
    h.update(input_source_fp.encode("utf-8"))
    if manifest_fp:
        h.update(b"|")
        h.update(manifest_fp.encode("utf-8"))
    source_fp = h.hexdigest()[:16]
    index_path = lib / ".process" / "资产索引.json"
    index_fp = file_fingerprint(index_path) or ""
    style_fp = _style_content_fingerprint(style_name) if style_name else ""
    artifacts: Dict[str, str] = {}
    for rel in [
        "反推_最终.md",
        "描述/人物描述.md",
        "描述/场景描述.md",
        "描述/道具描述.md",
        "提示词/基础/人物提示词.md",
        "提示词/基础/场景提示词.md",
        "提示词/基础/道具提示词.md",
        "提示词/衍生/人物提示词.md",
        "提示词/衍生/场景提示词.md",
        "提示词/衍生/道具提示词.md",
        "提示词/调度清单.md",
        "连续性/场景母版.md",
    ]:
        p = lib / rel
        fp = file_fingerprint(p)
        if fp:
            artifacts[rel] = fp
    return {
        "source_fingerprint": f"sha256:{source_fp}",
        "style_fingerprint": style_fp,
        "asset_index_hash": index_fp,
        "artifact_hashes": artifacts,
        "input_source_missing": input_source_missing,
    }


def archive_delivery_manifest(lib: Path) -> str | None:
    """把当前交付清单**复制**为历史快照（只增不改）；无清单返回 None。

    快照只作留档：`status` / `verify` 一律只读 `<资产库>/交付清单.json`，
    放进历史目录的清单永远不能被当成当前有效（否则旧签发可被反复复用）。
    用 write_bytes 复制：Windows 文本模式会把 \\n 翻成 \\r\\n，破坏逐字节留档。
    """
    live = Path(lib) / DELIVERY_MANIFEST_NAME
    if not live.is_file():
        return None
    stamp = ""
    try:
        data = json.loads(live.read_text(encoding="utf-8"))
        stamp = re.sub(r"[^0-9A-Za-z]+", "", str(data.get("generated_at") or ""))[:24]
    except (OSError, ValueError):
        stamp = ""
    if not stamp:
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    history = Path(lib) / DELIVERY_HISTORY_DIRNAME
    history.mkdir(parents=True, exist_ok=True)
    target = history / f"{stamp}.json"
    serial = 1
    while target.exists():
        target = history / f"{stamp}-{serial}.json"
        serial += 1
    target.write_bytes(live.read_bytes())
    return str(target)


def build_delivery_manifest(
    lib: Path,
    style: str | None,
    checks: List[Tuple[str, int]],
    counts: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """构建交付清单（verify 全绿后签发）。

    签发前置：启动证据必须可核验（E1 签发门）。缺失状态文件、原始输入不可读、
    或显式画风与启动状态画风不是同一张卡时抛 DeliveryRefused，绝不产出清单。
    """
    from datetime import datetime, timezone

    lib = Path(lib)
    ok, reason, _src, state_style = validate_start_evidence(lib, (style or "").strip())
    if not ok:
        raise DeliveryRefused(reason, evidence_detail(reason, state_style, style or ""))
    current = _current_fingerprints(lib)
    return {
        "schema_version": 1,
        "source_fingerprint": current["source_fingerprint"],
        "style_fingerprint": current["style_fingerprint"],
        "style": style or "",
        "asset_index_hash": current["asset_index_hash"],
        "artifact_hashes": current["artifact_hashes"],
        "counts": counts or {},
        "checks": [{"name": n, "rc": rc} for n, rc in checks],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "DELIVERED",
    }
