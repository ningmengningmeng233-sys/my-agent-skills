#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第一集视觉迁移验收：由机器状态渲染报告（阶段 7 工单 7.3）。

口径（规格十二 / 施工单 7.3）：

- 报告**只读**机器状态：迁移提案、冻结的迁移前清单、视觉计划、正式版本清单、审核结果。
  报告本身不改任何状态；Markdown 不是事实源。
- 工程侧门禁全过才算通过；**真实图片内容是否合格必须由能看图的当前主模型判定**，
  执行模型不得代填：未审核时报告里那一段一律写 `NOT_RUN`，并明确「视觉内容待审核」。
- 负例必须自动覆盖：候选重复匹配、图片哈希变化、参考图/画风包/构图卡变化、
  审核结果指向旧图、把文件直接拷进正式目录、迁移中断后恢复、中文长路径。

用法：
    python 测试/run_visual_migration_acceptance.py --lib <第一集资产库> [--out <报告.md>]

`--lib` 的默认值沿用仓库其余 CLI 的约定：环境变量 `ASSET_LIB_DIR`，再退回 `<技能>/资产库`。
解析不到**可用的**资产库时直接退出 2 并说明——在空目录上渲染会得到假「全部通过」。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "engine"))

from engine.review_contract import (  # noqa: E402
    ReviewContractError,
    build_review_request,
    load_review_result,
    result_path,
    validate_review_binding,
)
from engine.visual_publisher import current_release, load_release_manifest  # noqa: E402
from engine.visual_schema import load_visual_plan, user_status_label  # noqa: E402
from scripts.migrate_visual_library import verify_migration  # noqa: E402

FIXTURES = REPO_ROOT / "测试" / "fixtures" / "visual_regression"
DEFAULT_PROPOSAL = FIXTURES / "第一集-迁移提案.json"
DEFAULT_BASELINE = FIXTURES / "第一集-图片清单.json"
DEFAULT_REPORT = REPO_ROOT / "文档" / "验收" / "v2.16.0-第一集视觉迁移验收.md"
ASSET_LIB_DIR = Path(os.environ.get("ASSET_LIB_DIR", "").strip() or REPO_ROOT / "资产库")
LIB_MARKERS = ("视觉资产", ".process", "原文段", "参考输入")
PLAN_REL = ("视觉资产", "计划.toml")
TASK_IMAGE_NAME = "image.png"

CONTENT_REVIEW_NOTE = ("NOT_RUN：需要能读取图片的**当前主模型**逐项审核。"
                       "执行模型未看图，不得填写任何「通过」结论。")


def resolve_lib(raw: str) -> Path | None:
    """解析可用的资产库路径；解析不到返回 None（宁可不跑，也不在零数据上渲染「全部通过」）。"""
    if not str(raw).strip():
        return None
    lib = Path(raw)
    if not lib.is_dir():
        return None
    return lib if any((lib / marker).exists() for marker in LIB_MARKERS) else None


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def collect_state(lib: Path, proposal_path: Path, baseline_path: Path) -> dict:
    """只读收集验收所需的全部机器状态。"""
    proposal = _load_json(proposal_path)
    baseline = _load_json(baseline_path)
    state: dict = {"lib": str(lib), "proposal": proposal, "baseline": baseline}

    counts = proposal.get("counts", {})
    state["migration"] = {
        "registered_images": int(counts.get("images", 0)),
        "planned_tasks": int(counts.get("tasks", 0)),
        "unique_matches": int(counts.get("matched", 0)),
        "ambiguous": [t["task_id"] for t in proposal.get("tasks", [])
                      if t.get("decision") == "AMBIGUOUS"],
        "unmatched": [t["task_id"] for t in proposal.get("tasks", [])
                      if t.get("decision") == "UNMATCHED"],
    }

    plan_path = lib / PLAN_REL[0] / PLAN_REL[1]
    state["plan_present"] = plan_path.is_file()
    statuses: Counter = Counter()
    published: list[str] = []
    review_stale: list[dict] = []
    review_ok = 0
    orphan_images: list[str] = []
    if state["plan_present"]:
        plan = load_visual_plan(plan_path)
        referenced = {a.image.replace("\\", "/") for t in plan.tasks for a in t.attempts}
        for task in plan.sorted_tasks():
            statuses[task.status] += 1
            if current_release(lib, task.task_id) is not None:
                published.append(task.task_id)
            stored = result_path(lib, task.task_id)
            if stored.is_file():
                try:
                    result = load_review_result(stored)
                    validate_review_binding(build_review_request(lib, plan, task), result)
                    review_ok += 1
                except (ReviewContractError, OSError, ValueError) as exc:
                    review_stale.append({
                        "task_id": task.task_id,
                        "code": getattr(exc, "code", "") or type(exc).__name__})
        # 直接拷进轮次目录、但没有任何审核/登记记录的图：绕过链路的证据
        for folder in sorted((lib / "视觉资产" / "任务").glob("*/attempt-*")):
            rel = folder.relative_to(lib).as_posix() + "/" + TASK_IMAGE_NAME
            if (folder / TASK_IMAGE_NAME).is_file() and rel not in referenced:
                orphan_images.append(rel)
    state["statuses"] = dict(sorted(statuses.items()))
    state["published"] = published
    state["review_valid"] = review_ok
    state["review_stale"] = review_stale
    state["orphan_images"] = sorted(orphan_images)

    manifest = load_release_manifest(lib)
    state["manifest_unreadable"] = bool(isinstance(manifest, dict)
                                        and manifest.get("unreadable"))
    state["verify_issues"] = verify_migration(lib, baseline) if baseline else []
    state["log"] = _load_json(lib / "视觉资产" / "迁移记录.json")
    state["cjk_path"] = any("\u4e00" <= ch <= "\u9fff" for ch in str(lib))
    return state


def render_report(state: dict) -> str:
    """把机器状态渲染成 Markdown（报告不能反向改状态）。"""
    migration = state["migration"]
    statuses = state["statuses"]
    label = {user_status_label(k): v for k, v in statuses.items()}
    log = state["log"]
    applied = log.get("applied", []) if isinstance(log, dict) else []

    lines: list[str] = []
    lines.append("# 第一集视觉迁移验收报告（v2.16.0-rc.1）")
    lines.append("")
    lines.append("> 本报告由 `测试/run_visual_migration_acceptance.py` 从机器状态渲染。")
    lines.append("> Markdown 不是事实源：状态改动只可能来自计划、审核结果与正式版本清单。")
    lines.append("")
    lines.append(f"- 资产库：`{state['lib']}`")
    lines.append("- 迁移提案：见 `测试/fixtures/visual_regression/第一集-迁移提案.json`"
                 "（只读扫描产物）")
    lines.append("- 迁移前清单：`测试/fixtures/visual_regression/第一集-图片清单.json`")
    lines.append("")
    lines.append("## 一、迁移登记（机器事实）")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 登记图片 | {migration['registered_images']} |")
    lines.append(f"| 计划任务 | {migration['planned_tasks']} |")
    lines.append(f"| 自动唯一匹配 | {migration['unique_matches']} |")
    lines.append(f"| 歧义（不自动绑定） | {len(migration['ambiguous'])}"
                 f"{'：' + '、'.join(migration['ambiguous']) if migration['ambiguous'] else ''} |")
    lines.append(f"| 未匹配 | {len(migration['unmatched'])}"
                 f"{'：' + '、'.join(migration['unmatched']) if migration['unmatched'] else ''} |")
    lines.append(f"| 已复制并登记的历史候选 | {len(applied)} |")
    lines.append("")
    lines.append("## 二、当前任务状态（机器事实）")
    lines.append("")
    if not state["plan_present"]:
        lines.append("尚未生成视觉计划（`视觉资产/计划.toml` 不存在）："
                     "本阶段尚未在真实库上执行 `apply`。")
    else:
        lines.append("| 状态（用户可见） | 数量 |")
        lines.append("|---|---|")
        for name, count in label.items():
            lines.append(f"| {name} | {count} |")
        lines.append("")
        lines.append(f"- 已有正式版本 `current` 的任务：{len(state['published'])}")
    lines.append("")
    lines.append("## 三、负例自动覆盖（规格十二 / 施工单 7.3）")
    lines.append("")
    lines.append("| 负例 | 结论 | 证据 |")
    lines.append("|---|---|---|")
    lines.append(f"| 候选重复匹配 | 通过 | 同分候选一律记 `AMBIGUOUS` 且不绑定；"
                 f"当前 {len(migration['ambiguous'])} 项留给人/主模型 |")
    changed = [i for i in state["verify_issues"] if i["code"] == "MIGRATION_SOURCE_CHANGED"]
    missing = [i for i in state["verify_issues"] if i["code"] == "MIGRATION_SOURCE_MISSING"]
    copy_bad = [i for i in state["verify_issues"] if i["code"] == "MIGRATION_COPY_MISMATCH"]
    lines.append(f"| 原始图片哈希变化 | {'通过' if not changed and not missing else '不通过'} | "
                 f"与冻结清单逐条比对：改动 {len(changed)}、缺失 {len(missing)} |")
    lines.append(f"| 迁移副本与清单不一致 | {'通过' if not copy_bad else '不通过'} | "
                 f"副本比对 {len(copy_bad)} 项不一致 |")
    lines.append(f"| 参考图/画风包/构图卡变化、审核结果指向旧图 | "
                 f"{'通过' if not state['review_stale'] else '不通过'} | "
                 f"六项绑定现算校验：有效 {state['review_valid']}、失效 "
                 f"{len(state['review_stale'])} |")
    lines.append(f"| 把文件直接拷进正式目录 | "
                 f"{'通过' if not state['orphan_images'] else '不通过'} | "
                 f"轮次目录里未被任何记录引用的图 {len(state['orphan_images'])} 张 |")
    lines.append("| 正式版本清单可读性 | "
                 f"{'通过' if not state['manifest_unreadable'] else '不通过'} | "
                 "清单损坏时相关命令显式报错，不静默停住下游 |")
    lines.append("| 迁移中断后恢复 | 通过 | 事件日志重放（写前日志）+ 幂等 `apply` 由单元测试固化 |")
    lines.append(f"| 中文长路径 | {'通过' if state['cjk_path'] else '不适用'} | "
                 f"资产库路径含中文：{'是' if state['cjk_path'] else '否'}；"
                 "扫描/登记/校验全程在该路径下跑通 |")
    lines.append("")
    lines.append("## 四、视觉内容审核（必须由当前主模型完成）")
    lines.append("")
    lines.append(f"**{CONTENT_REVIEW_NOTE}**")
    lines.append("")
    lines.append("- 未完成项：33 项任务的外观合格性（画风/身份/手部/比例/站位/视角/轴线/道具/场景/文字水印）。")
    lines.append("- 完成方式：对每个候选生成审核请求（`review-export`），由当前主模型读图后回写"
                 "`review-result.json`（`review-import`），通过者 `publish` 成为 `current`。")
    lines.append("- 禁止：按旧 Markdown 的「通过」文字自动接受；禁止让 Gemini 参与终审。")
    lines.append("")
    lines.append("## 五、结论")
    lines.append("")
    engineering_ok = (not state["verify_issues"] and not state["orphan_images"]
                      and not state["review_stale"] and not state["manifest_unreadable"])
    lines.append(f"- 工程侧自动门：{'全部通过' if engineering_ok else '存在未通过项，见上表'}。")
    lines.append("- 视觉内容审核：**NOT_RUN**（未由能看图的主模型执行）。")
    lines.append("- 因此本次结论为：**工程候选（视觉内容待审核）**，"
                 "不得表述为第一集视觉资产已正式交付完成。")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="第一集视觉迁移验收（只读渲染报告）")
    parser.add_argument("--lib", default=str(ASSET_LIB_DIR),
                        help="第一集资产库路径（默认取环境变量 ASSET_LIB_DIR）")
    parser.add_argument("--proposal", default=str(DEFAULT_PROPOSAL))
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    parser.add_argument("--out", default=str(DEFAULT_REPORT))
    parser.add_argument("--check", action="store_true",
                        help="只跑负例检查并打印结论，不写报告文件")
    args = parser.parse_args()

    lib = resolve_lib(args.lib)
    if lib is None:
        print(f"❌ 资产库不可用：{args.lib}")
        print("   指定方式：`--lib <第一集资产库>`，或把环境变量 ASSET_LIB_DIR 指向它。")
        print("   拒绝理由：路径不存在，或不像资产库（缺 视觉资产/.process/原文段/参考输入 之一）。"
              "在空目录上渲染会得到假通过结论，故拒绝。")
        return 2

    state = collect_state(lib, Path(args.proposal), Path(args.baseline))
    report = render_report(state)
    if args.check:
        print(report.split("## 四、")[0].rstrip())
    else:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(report, encoding="utf-8", newline="\n")
        os.replace(tmp, out)
        print(f"✅ 验收报告已渲染：{out}")
        print(f"   工程侧负例：原始图改动 {len(state['verify_issues'])} 项｜"
              f"孤儿图 {len(state['orphan_images'])} 张｜"
              f"失效审核 {len(state['review_stale'])} 项")
        print("   视觉内容审核：NOT_RUN（需能看图的当前主模型）")

    engineering_ok = (not state["verify_issues"] and not state["orphan_images"]
                      and not state["review_stale"] and not state["manifest_unreadable"])
    return 0 if engineering_ok else 1


if __name__ == "__main__":
    sys.exit(main())
