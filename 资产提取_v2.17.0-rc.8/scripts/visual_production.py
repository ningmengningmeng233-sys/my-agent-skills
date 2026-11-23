#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉生产 CLI（工单03-Task7）。

用法：
    python scripts/visual_production.py run    --lib <资产库> --task <任务ID>
    python scripts/visual_production.py export --lib <资产库> --task <任务ID>
    python scripts/visual_production.py import --lib <资产库> --task <任务ID> --image <图片>
    python scripts/visual_production.py check  --lib <资产库> [--task <任务ID>] [--qc <结果文件>]

口径：无 `GEMINI_API_KEY` 时 `run` 明确失败并提示改用手工导出；`export` 永远可用。
导入必须绑定计划中已存在的任务 ID；只生成依赖已通过的任务。
离线开关：`ASSET_OFFLINE=1` 时生成类外呼在任何 socket 之前本地拒绝
（不隐式回退包外 .env、不改计划、不留事件），传输层抛 `OfflineRequested`。
"""
import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))

from engine.image_executor import (  # noqa: E402
    GenerationError,
    ManualExportExecutor,
    executor_is_configured,
)
from engine.image_qc import parse_qc_response, write_qc_result  # noqa: E402
from engine.offline import OFFLINE_ENV, OfflineRequested, offline_requested  # noqa: E402
from engine.visual_request_chain import (  # noqa: E402
    VisualCompileError,
    compile_for_library,
)
from engine.visual_schema import (  # noqa: E402
    load_visual_plan,
    user_status_label,
    write_visual_plan,
)
from engine.visual_store import StoreError, import_manual_result  # noqa: E402

# 视觉 CLI 也必须遵循统一配置来源：ASSET_ENV_PATH → 技能目录 .env → ~/.asset-extract/.env
# （进程环境优先级最高；只加载配置，绝不打印或记录任何密钥值）
from engine.common import load_env as _load_env  # noqa: E402

_load_env()

PLAN_REL = ("视觉资产", "计划.toml")
INDEX_REL = (".process", "资产索引.json")
PACKAGE_REL = ("视觉资产", "任务包")


def _load_plan(lib: Path):
    path = lib / PLAN_REL[0] / PLAN_REL[1]
    if not path.is_file():
        print(f"❌ 视觉计划不存在: {path}（先跑 maintenance.py visual-plan）")
        return None, None
    try:
        return load_visual_plan(path), path
    except (OSError, ValueError) as e:
        print(f"❌ 视觉计划不可解析：{e}")
        return None, path


def _asset(lib: Path, asset_id: str) -> dict:
    path = lib / INDEX_REL[0] / INDEX_REL[1]
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for item in data.get("assets", []):
        if isinstance(item, dict) and item.get("asset_id") == asset_id:
            return item
    return {}


def _blocked_by_upstream(plan, task) -> list[str]:
    by_id = {t.task_id: t for t in plan.tasks}
    return [d for d in task.depends_on
            if (by_id.get(d) is None or not by_id[d].is_accepted)]


def _composition_gate(lib: Path, task) -> bool:
    """构图预览模式：未批准（或构图卡已改）时拒绝进入生成/导出。"""
    from engine.director_composition import require_composition_approval
    from engine.project_settings import safe_settings_value

    try:
        require_composition_approval(lib, task, safe_settings_value(lib, "composition_mode"))
    except PermissionError as e:
        print(f"❌ {e}")
        print("   先看构图卡：python scripts/visual_production.py compose --lib <资产库>")
        return False
    return True


def _export_task(lib: Path, plan, task):
    """导出单个任务包：返回（更新后的任务 或 None, 结果码）。不写计划（由调用方统一写）。"""
    blocked = _blocked_by_upstream(plan, task)
    if blocked:
        print(f"❌ 任务 {task.task_id} 依赖未通过：{'、'.join(blocked)}（先通过上游再导出）")
        return None, 1
    if not _composition_gate(lib, task):
        return None, 1
    try:
        compiled = compile_for_library(task, lib, plan)
    except VisualCompileError as e:
        print(f"❌ 参考图/风格包编译失败（{e.code}）：{e.detail}")
        print("   已阻止导出：参考图未通过校验时不得降级为空参考继续。")
        return None, 1
    executor = ManualExportExecutor(lib / PACKAGE_REL[0] / PACKAGE_REL[1])
    result = executor.generate(compiled.request, resolved=compiled.resolved,
                               omitted=compiled.omitted)
    from dataclasses import replace as _replace

    updated = _replace(task, status="AWAITING_MANUAL_RESULT",
                       notes=(task.notes or "") + "｜已导出手工任务包")
    package = lib / PACKAGE_REL[0] / PACKAGE_REL[1] / task.task_id
    print(f"✅ 手工任务包已导出: {package}")
    print(f"   参考图 {len(compiled.resolved)} 张（含职责与哈希清单），"
          f"被省略 {len(compiled.omitted)} 张；包内自带 参考图/ 可跨机使用。")
    _ = result
    return updated, 0


def cmd_export(lib: Path, task_id: str) -> int:
    plan, plan_path = _load_plan(lib)
    if plan is None:
        return 1
    task = plan.by_id(task_id)
    if task is None:
        print(f"❌ 计划中没有任务 {task_id}（导入/导出必须绑定已有 task_id）")
        return 1
    updated, rc = _export_task(lib, plan, task)
    if updated is None:
        return rc
    write_visual_plan(plan.with_task(updated), plan_path)
    print(f"   python scripts/visual_production.py import --lib <资产库> --task {task_id} --image <图片>")
    return 0


def cmd_export_all(lib: Path) -> int:
    """`export-all`：为所有**当前可执行**任务导出完整任务包（只准备提示词，零网络）。"""
    plan, plan_path = _load_plan(lib)
    if plan is None:
        return 1
    exported = 0
    skipped = 0
    for task in plan.sorted_tasks():
        if task.is_accepted or task.status == "AWAITING_MANUAL_RESULT":
            skipped += 1
            continue
        if _blocked_by_upstream(plan, task):
            skipped += 1
            continue
        updated, rc = _export_task(lib, plan, task)
        if updated is None:
            return rc
        plan = plan.with_task(updated)
        exported += 1
    write_visual_plan(plan, plan_path)
    print(f"✅ 本次导出 {exported} 项；跳过 {skipped} 项（已通过/等待回传/依赖未通过）。")
    print("   这是零网络路径：不调用任何图片 API；回传结果用 import，随后走主模型审核与发布。")
    return 0


def cmd_import(lib: Path, task_id: str, image: Path) -> int:
    plan, plan_path = _load_plan(lib)
    if plan is None:
        return 1
    task = plan.by_id(task_id)
    if task is None:
        print(f"❌ 计划中没有任务 {task_id}（导入必须绑定已有 task_id）")
        return 1
    try:
        record = import_manual_result(lib, task_id, image, existing=task.attempts)
    except StoreError as e:
        print(f"❌ 导入失败（{e.code}）：{e.detail}")
        return 1
    from dataclasses import replace as _replace

    updated = _replace(task, attempts=(*task.attempts, record)) if record not in task.attempts \
        else task
    updated = _replace(updated, status="GENERATED")
    write_visual_plan(plan.with_task(updated), plan_path)
    print(f"✅ 已导入第 {record.round} 轮产物: {record.image}")
    print(f"   下一步: python scripts/visual_production.py check --lib <资产库> --task {task_id}"
          " --qc <结构化检查结果>")
    return 0


def cmd_check(lib: Path, task_id: str | None, qc_file: Path | None) -> int:
    plan, plan_path = _load_plan(lib)
    if plan is None:
        return 1
    if task_id is None:
        print(f"🎬 视觉计划：{plan.style_name}@{plan.style_version}｜任务 {len(plan.tasks)} 项")
        for task in plan.sorted_tasks():
            print(f"   {task.task_id}｜{user_status_label(task.status)}"
                  f"｜轮次 {task.round_count}/{task.max_rounds}")
        return 0
    task = plan.by_id(task_id)
    if task is None:
        print(f"❌ 计划中没有任务 {task_id}")
        return 1
    if qc_file is None:
        print(f"任务 {task_id}｜{user_status_label(task.status)}｜轮次 {task.round_count}/{task.max_rounds}")
        for attempt in task.attempts:
            print(f"   第 {attempt.round} 轮: {attempt.image}")
        print("   写回检查结果：--qc <结构化 JSON/TOML>")
        return 0
    if not qc_file.is_file():
        print(f"❌ 检查结果文件不存在: {qc_file}")
        return 1
    try:
        data = json.loads(qc_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"❌ 检查结果必须是结构化 JSON：{e}")
        return 1
    attempt = task.latest_attempt
    if attempt is None:
        print(f"❌ 任务 {task_id} 尚无产物，无法写回检查结果")
        return 1
    result = parse_qc_response(data, expected={"task_id": task_id,
                                               "input_hashes": [attempt.image_sha256]})
    write_qc_result(lib, attempt, result)
    status = {"ACCEPTED": "READY_AS_REFERENCE", "NEEDS_REVISION": "NEEDS_REVISION",
              "NEEDS_HUMAN_DECISION": "NEEDS_HUMAN_DECISION",
              "QC_UNTRUSTWORTHY": "NEEDS_REVIEW"}.get(result.decision, task.status)
    from dataclasses import replace as _replace

    write_visual_plan(plan.with_task(_replace(task, status=status)), plan_path)
    print(f"✅ 检查结果已写回：{result.decision} → 任务状态 {user_status_label(status)}")
    if result.untrustworthy_reason:
        print(f"   ⚠️ {result.untrustworthy_reason}")
    return 0


def cmd_run(lib: Path, task_id: str) -> int:
    # 项目设置先于一切：用户显式选择的模式决定这条路能不能走。
    # 必须排在读计划之前——data_only 项目本就没有计划文件，先读计划会把
    # 「模式不允许生图」误报成「计划不存在」，用户拿不到可行动的理由。
    if not _mode_gate(
            lib, export_hint=f"python scripts/visual_production.py export "
                             f"--lib <资产库> --task {task_id}"):
        return 1
    plan, plan_path = _load_plan(lib)
    if plan is None:
        return 1
    task = plan.by_id(task_id)
    if task is None:
        print(f"❌ 计划中没有任务 {task_id}")
        return 1
    blocked = _blocked_by_upstream(plan, task)
    if blocked:
        print(f"❌ 任务 {task_id} 依赖未通过：{'、'.join(blocked)}（只生成依赖已通过的任务）")
        return 1
    if not _composition_gate(lib, task):
        return 1
    if not executor_is_configured():
        print("❌ 未配置 GEMINI_API_KEY：自动生成不可用。")
        print(f"   请设置环境变量 GEMINI_API_KEY，或改用手工导出：python scripts/visual_production.py export "
              f"--lib <资产库> --task {task_id}")
        return 1
    if offline_requested():
        # 离线开关在任何网络调用之前本地拒绝：不改计划、不派发、不留事件。
        print(f"❌ OFFLINE_BLOCKED：离线模式（{OFFLINE_ENV}）已置真，拒绝发起生成外呼。")
        print(f"   要真实生成请取消 {OFFLINE_ENV} 并设置环境变量 GEMINI_API_KEY；"
              f"或改用手工导出：python scripts/visual_production.py export "
              f"--lib <资产库> --task {task_id}")
        return 1
    from engine.gemini_image import GeminiImageExecutor, load_gemini_config
    from engine.visual_pipeline import run_task

    try:
        compiled = compile_for_library(task, lib, plan)
    except VisualCompileError as e:
        print(f"❌ 参考图/风格包编译失败（{e.code}）：{e.detail}")
        print("   已阻止生成：参考图未通过校验时不得降级为空参考发请求。")
        return 1
    config = load_gemini_config()
    executor = GeminiImageExecutor(config=config)

    try:
        # 正式路径只生成到「等待审核」：Gemini 永不终审（阶段 6 工单 6.4）
        updated = run_task(task, executor, None, lib, plan=plan, request=compiled.request)
    except GenerationError as e:
        print(f"❌ 生成失败（{e.code}）：{e.detail}")
        return 1
    except OfflineRequested as e:
        # 传输层本地拦截（任何 socket 之前）：不落任何产物、不改计划、不留事件。
        print(f"❌ {e.code}：{e.detail}")
        return 1
    write_visual_plan(plan.with_task(updated) if updated.task_id == task_id else plan, plan_path)
    print(f"✅ 任务 {task_id} → {user_status_label(updated.status)}"
          f"（轮次 {updated.round_count}/{updated.max_rounds}）")
    print("   下一步：把待审图片交给**当前主模型**审核——"
          f"python scripts/visual_production.py review-import --lib <资产库> --task {task_id} "
          "--result <审核结果.json>")
    return 0


def cmd_compose(lib: Path, task_id: str | None) -> int:
    """`compose`：为计划内任务生成/刷新导演构图卡，并把路径与哈希写回计划。"""
    plan, plan_path = _load_plan(lib)
    if plan is None:
        return 1
    from engine.director_composition import (
        composition_path,
        derive_composition,
        write_composition_card,
    )

    targets = ([plan.by_id(task_id)] if task_id else list(plan.sorted_tasks()))
    if task_id and targets[0] is None:
        print(f"❌ 计划中没有任务 {task_id}")
        return 1
    written = 0
    for task in targets:
        asset = _asset(lib, task.asset_id)
        card = derive_composition(task, asset)
        # 已有卡片且内容一致时不改写（幂等），只补计划的路径与哈希
        path = composition_path(lib, task.task_id)
        text = card.to_toml()
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            pass
        else:
            write_composition_card(lib, card)
            written += 1
        import hashlib

        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rel = path.relative_to(lib).as_posix()
        if (task.composition_path, task.composition_sha256) != (rel, digest):
            from dataclasses import replace as _replace

            plan = plan.with_task(_replace(task, composition_path=rel,
                                           composition_sha256=digest))
    write_visual_plan(plan, plan_path)
    print(f"✅ 构图卡已就绪：{len(targets)} 项（新写/更新 {written} 张）")
    print("   预览模式（composition=preview）下需批准后才能生成："
          "python scripts/visual_production.py approve-composition --lib <资产库> --task <任务ID>")
    return 0


def cmd_approve_composition(lib: Path, task_id: str) -> int:
    """`approve-composition`：批准构图卡（记录哈希；改卡即失效）。"""
    plan, plan_path = _load_plan(lib)
    if plan is None:
        return 1
    task = plan.by_id(task_id)
    if task is None:
        print(f"❌ 计划中没有任务 {task_id}")
        return 1
    from engine.director_composition import approve_composition

    try:
        path = approve_composition(lib, task_id)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return 1
    print(f"✅ 已批准构图卡：{task_id}")
    print(f"   批准记录：{path}（构图卡内容一旦变化即自动失效，须重新批准）")
    _ = plan_path
    return 0


def cmd_review_export(lib: Path, task_id: str) -> int:
    """`review-export`：导出结构化审核请求，交给**当前主模型**读图审核。"""
    plan, plan_path = _load_plan(lib)
    if plan is None:
        return 1
    task = plan.by_id(task_id)
    if task is None:
        print(f"❌ 计划中没有任务 {task_id}")
        return 1
    from engine.review_contract import ReviewContractError
    from engine.review_provider import FileReviewProvider

    provider = FileReviewProvider(lib, plan)
    try:
        path = provider.prepare(task)
    except ReviewContractError as e:
        print(f"❌ 无法生成审核请求（{e.code}）：{e.detail}")
        return 1
    print(f"✅ 审核请求已导出：{path}")
    print("   主模型请读图后按逐项清单写出 review-result.json（判定只允许 "
          "ACCEPTED / PASS_WITH_NOTES / NEEDS_REVISION / NEEDS_HUMAN_DECISION）。")
    print(f"   回写：python scripts/visual_production.py review-import --lib <资产库> "
          f"--task {task_id} --result <审核结果.json>")
    _ = plan_path
    return 0


def cmd_review_import(lib: Path, task_id: str, result_file: Path) -> int:
    """`review-import`：导入主模型审核结果并校验六项绑定，再写回任务状态。"""
    plan, plan_path = _load_plan(lib)
    if plan is None:
        return 1
    task = plan.by_id(task_id)
    if task is None:
        print(f"❌ 计划中没有任务 {task_id}")
        return 1
    from dataclasses import replace as _replace

    from engine.review_contract import ReviewContractError, load_review_result
    from engine.review_provider import import_review_decision

    if not Path(result_file).is_file():
        print(f"❌ 审核结果不存在：{result_file}")
        return 1
    try:
        result = load_review_result(Path(result_file))
        from engine.review_contract import result_path as _result_path

        stored = _result_path(lib, task_id)
        if Path(result_file).resolve() != stored.resolve():
            stored.parent.mkdir(parents=True, exist_ok=True)
            stored.write_text(Path(result_file).read_text(encoding="utf-8"),
                              encoding="utf-8", newline="\n")
        from engine.review_provider import FileReviewProvider

        # 校验结果确实针对**当前输入**：六项绑定按当前事实现算比对，不符即拒绝写回
        FileReviewProvider(lib, plan).collect(task)
        updated = import_review_decision(plan, task, result)
    except ReviewContractError as e:
        print(f"❌ 审核结果被拒绝（{e.code}）：{e.detail}")
        print("   绑定不成立的结果不得写回状态；请重新审核或修正输入后重出请求。")
        return 1
    write_visual_plan(plan.with_task(_replace(updated)), plan_path)
    print(f"✅ 已写回审核判定：{result.decision} → {user_status_label(updated.status)}")
    if updated.is_accepted:
        print(f"   发布正式版本：python scripts/visual_production.py publish "
              f"--lib <资产库> --task {task_id}")
    return 0


def cmd_publish(lib: Path, task_id: str) -> int:
    """`publish`：把已通过的审核结果发布为正式版本（只有发布器能写正式版本清单）。"""
    plan, plan_path = _load_plan(lib)
    if plan is None:
        return 1
    task = plan.by_id(task_id)
    if task is None:
        print(f"❌ 计划中没有任务 {task_id}")
        return 1
    from engine.review_contract import (
        ReviewContractError,
        build_review_request,
        load_review_result,
        result_path,
    )
    from engine.visual_publisher import PublishError, publish_task

    stored = result_path(lib, task_id)
    if not stored.is_file():
        print(f"❌ 还没有审核结果：{stored}（先 review-export 交给主模型，再 review-import）")
        return 1
    try:
        result = load_review_result(stored)
        request = build_review_request(lib, plan, task)
        plan, record = publish_task(lib, plan, task, request, result)
    except (ReviewContractError, PublishError) as e:
        code = getattr(e, "code", "PUBLISH_FAILED")
        print(f"❌ 发布被拒绝（{code}）：{getattr(e, 'detail', e)}")
        return 1
    write_visual_plan(plan, plan_path)
    print(f"✅ 已发布正式版本：{task_id} → {record.version}"
          f"（{record.mime} {record.width}x{record.height}）")
    print("   替换当前版本时旧版自动进入历史；直接下游任务转为需要重新检查。")
    return 0


def _mode_gate(lib: Path, *, export_hint: str) -> bool:
    """模式门（项目设置先于一切网络/密钥检查）：返回是否允许继续自动生成。

    输出文案与 `cmd_run` 原样一致，确保既有用户与测试口径不变。
    """
    sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
    from engine.project_settings import (
        MODE_BLOCKED_DATA_ONLY,
        MODE_BLOCKED_PROMPT_ONLY,
        ProjectSettingsError,
        explicit_visual_mode,
    )

    try:
        mode = explicit_visual_mode(lib)
    except ProjectSettingsError as e:
        print(f"❌ {e.code}：{e.detail}")
        return False
    if mode == "prompt_only":
        print(f"❌ 已阻止自动生成（{MODE_BLOCKED_PROMPT_ONLY}）：本项目设置为「不直接生成，"
              "只准备提示词」。")
        print(f"   请改用：{export_hint}")
        return False
    if mode == "data_only":
        print(f"❌ 已阻止自动生成（{MODE_BLOCKED_DATA_ONLY}）：本项目设置为「只反推资产、不生图」。")
        print("   如需生图：python scripts/maintenance.py configure-visual "
              "--lib <资产库> --mode prompt-only｜direct-generate")
        return False
    return True


def _pause_gate(lib: Path) -> bool:
    """暂停门：暂停期间不派发任何新任务（已落盘状态一律不动）。"""
    from engine.visual_scheduler import is_paused, pause_path

    if not is_paused(lib):
        return True
    print(f"❌ 已暂停（VISUAL_PAUSED）：{pause_path(lib)} 存在，暂停期间不派发新任务。")
    print("   继续：python scripts/visual_production.py pause --lib <资产库> --clear")
    return False


def _no_key_refusal(export_hint: str) -> int:
    print("❌ 未配置 GEMINI_API_KEY：自动生成不可用。")
    print("   请设置环境变量 GEMINI_API_KEY，或" + export_hint + " 手工导出提示词。")
    print(f"   （密钥来源：ASSET_ENV_PATH → 技能目录 .env → ~/.asset-extract/.env；"
          f"{OFFLINE_ENV} 置真时隐式包外回退被屏蔽，且任何外呼一律本地拒绝）")
    return 1


def _effective_parallel(lib: Path, override: int | None) -> int:
    """并发数：命令行 > 项目设置 > 默认 3（只接受 1–4）。"""
    if override is not None:
        return int(override)
    from engine.project_settings import DEFAULT_MAX_PARALLEL, load_project_settings

    try:
        return int(load_project_settings(lib).max_parallel)
    except (OSError, ValueError):
        return int(DEFAULT_MAX_PARALLEL)


def _run_scheduler(lib: Path, *, parallel: int | None, resume: bool) -> int:
    """run-ready / resume 的公共实现：先过门，再看有没有可执行任务，最后才构造执行器。"""
    if parallel is not None and not 1 <= int(parallel) <= 4:
        print(f"❌ SCHEDULER_PARALLEL_INVALID：并发数只支持 1–4（当前 {parallel}）")
        return 1
    if not _pause_gate(lib):
        return 1
    export_hint = "python scripts/visual_production.py export-all --lib <资产库>"
    if not _mode_gate(lib, export_hint=export_hint):
        return 1
    plan, _plan_path = _load_plan(lib)
    if plan is None:
        return 1
    from engine.visual_scheduler import SchedulerError, VisualScheduler

    scheduler = VisualScheduler(lib, max_parallel=_effective_parallel(lib, parallel))
    try:
        ready = scheduler.ready_tasks(plan)
    except SchedulerError as e:
        print(f"❌ {e.code}：{e.detail}")
        return 1
    if not ready:
        print("✅ 没有可执行任务（依赖未通过、已通过或已阻断的任务不会被派发）。")
        print("   查看状态：python scripts/visual_production.py status --lib <资产库>")
        return 0
    if not executor_is_configured():
        return _no_key_refusal(export_hint)
    if offline_requested():
        # 离线开关在任何网络调用之前本地拒绝：此处尚未派发，计划与事件日志不动。
        print(f"❌ OFFLINE_BLOCKED：离线模式（{OFFLINE_ENV}）已置真，拒绝派发任何生成外呼。")
        print(f"   要真实生成请取消 {OFFLINE_ENV} 并设置环境变量 GEMINI_API_KEY；"
              f"或改用：{export_hint} 手工导出提示词。")
        return 1
    from engine.gemini_image import GeminiImageExecutor, load_gemini_config

    # 内层重试交给调度器的传输策略统一处理：否则最坏调用次数会变成「外层 × 内层」乘积
    scheduler.executor = GeminiImageExecutor(config=replace(load_gemini_config(),
                                                            max_retries=1))
    try:
        summary = scheduler.resume() if resume else scheduler.run_ready(plan)
    except SchedulerError as e:
        print(f"❌ {e.code}：{e.detail}")
        return 1
    except OfflineRequested as e:
        print(f"❌ {e.code}：{e.detail}")
        return 1
    _print_summary(summary, ready_count=len(ready))
    return 0


def _print_summary(summary, *, ready_count: int) -> None:
    payload = summary.to_dict()
    print(f"✅ 本轮完成：生成 {payload['generated']}｜发布 {payload['published']}｜"
          f"复用 {payload['reused']}｜等审核 {payload['waiting_review']}｜"
          f"等上游 {payload['waiting_upstream']}｜阻断 {payload['blocked']}｜"
          f"传输失败 {payload['transport_failed']}")
    if payload["recovered"]:
        print(f"   崩溃恢复：已从事件日志补齐 {payload['recovered']} 项状态（未重复生成）")
    if payload["rounds_exhausted"]:
        print(f"   已达最大轮次转人工决定：{payload['rounds_exhausted']} 项")
    for issue in payload["review_issues"]:
        print(f"   ⚠️ 审核结果未通过校验：{issue}")
    if payload["paused"]:
        print("   已暂停：本轮不再派发新任务（已落盘状态保留）。")
    print(f"   计划修订 {payload['revision']}｜本轮派发前可执行 {ready_count} 项"
          f"｜并发 {payload['parallel_start']}→{payload['parallel_end']}"
          + (f"（传输重试 {payload['transport_retries']} 次）"
             if payload["transport_retries"] else ""))
    print("   下一步：把待审图片交给**当前主模型**审核——"
          "python scripts/visual_production.py review-export --lib <资产库> --task <任务ID>")


def cmd_run_ready(lib: Path, parallel: int | None = None) -> int:
    """`run-ready`：按依赖图批量生成当前可执行任务，不再要求逐个 `--task`。"""
    return _run_scheduler(lib, parallel=parallel, resume=False)


def cmd_resume(lib: Path, parallel: int | None = None) -> int:
    """`resume`：崩溃后从事件日志恢复，并继续调度剩余任务。"""
    return _run_scheduler(lib, parallel=parallel, resume=True)


def cmd_pause(lib: Path, clear: bool = False) -> int:
    """`pause`：暂停后续派发（`--clear` 解除）；只写标记，不动计划与产物。"""
    from engine.visual_scheduler import clear_pause, is_paused, pause_path, pause_visual

    if clear:
        removed = clear_pause(lib)
        print("✅ 已解除暂停。" if removed else "ℹ️ 当前未处于暂停状态。")
        return 0
    if is_paused(lib):
        print(f"ℹ️ 已处于暂停状态：{pause_path(lib)}")
        return 0
    path = pause_visual(lib, reason="命令行暂停")
    print(f"✅ 已暂停后续派发（{path}）；已落盘的计划与产物未改动。")
    print("   继续：python scripts/visual_production.py pause --lib <资产库> --clear")
    return 0


def cmd_status(lib: Path, as_json: bool = False) -> int:
    """`status`：人话状态，`--json` 给机器读（机器码只出现在 JSON 与详细日志里）。"""
    from engine.project_settings import MODE_LABELS, safe_visual_mode
    from engine.visual_publisher import current_release
    from engine.visual_scheduler import VisualScheduler, is_paused

    plan_path = lib / PLAN_REL[0] / PLAN_REL[1]
    mode = safe_visual_mode(lib)
    paused = is_paused(lib)
    if not plan_path.is_file():
        payload = {"lib": str(lib), "plan_path": str(plan_path), "enabled": False,
                   "mode": mode, "paused": paused, "revision": 0, "total": 0,
                   "counts": {}, "counts_label": {}, "ready": [], "waiting_upstream": [],
                   "waiting_review": [], "published": [], "rounds_exhausted": []}
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"📋 视觉生产未启用：没有计划文件（{plan_path}）")
            print(f"   模式：{MODE_LABELS.get(mode, mode)}｜暂停：{'是' if paused else '否'}")
        return 0
    try:
        plan = load_visual_plan(plan_path)
    except (OSError, ValueError) as e:
        print(f"❌ 计划不可读：{type(e).__name__}")
        return 1
    scheduler = VisualScheduler(lib)
    counts = plan.counts_by_status()
    from engine.visual_scheduler import SchedulerError

    try:
        ready = [t.task_id for t in scheduler.ready_tasks(plan)]
        waiting_upstream = [t.task_id for t in scheduler.waiting_upstream_tasks(plan)]
        exhausted = [t.task_id for t in scheduler.rounds_exhausted_tasks(plan)]
    except SchedulerError as e:
        # 正式版本清单损坏等硬故障必须显式报错：静默当成「没有已发布」会把下游永久停住
        print(f"❌ {e.code}：{e.detail}")
        return 1
    payload = {
        "lib": str(lib), "plan_path": str(plan_path), "enabled": True, "mode": mode,
        "paused": paused, "revision": int(plan.revision), "total": len(plan.tasks),
        "max_parallel": _effective_parallel(lib, None),
        "counts": counts,
        "counts_label": {user_status_label(k): v for k, v in counts.items()},
        "ready": ready,
        "waiting_upstream": waiting_upstream,
        "waiting_review": [t.task_id for t in plan.tasks
                           if t.status == "WAITING_REVIEW"],
        "published": [t.task_id for t in plan.tasks
                      if current_release(lib, t.task_id) is not None],
        "rounds_exhausted": exhausted,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"📋 视觉计划：{plan_path}")
    print(f"   模式：{MODE_LABELS.get(mode, mode)}｜暂停：{'是' if paused else '否'}｜"
          f"修订 {payload['revision']}")
    labels = "、".join(f"{name} {count}" for name, count in payload["counts_label"].items())
    print(f"   任务 {payload['total']} 项：{labels or '（空）'}")
    print(f"   现在可执行 {len(payload['ready'])} 项"
          + (f"：{'、'.join(payload['ready'])}" if payload["ready"] else ""))
    print(f"   等上游 {len(payload['waiting_upstream'])} 项｜"
          f"等待审核 {len(payload['waiting_review'])} 项｜"
          f"已发布 {len(payload['published'])} 项")
    if payload["rounds_exhausted"]:
        print(f"   已达最大轮次待人工决定：{'、'.join(payload['rounds_exhausted'])}")
    if paused:
        print("   暂停中：python scripts/visual_production.py pause --lib <资产库> --clear")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="视觉生产：run / export / import / check / compose / approve-composition")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("run", "调用 Gemini 生成（需密钥）"),
                            ("export", "导出手工任务包（无密钥可用）"),
                            ("import", "导入手工生成结果"),
                            ("check", "查看任务或写回质检结果"),
                            ("compose", "生成/刷新导演构图卡（预览模式下先看卡再生成）"),
                            ("approve-composition", "批准构图卡（记录哈希，改卡即失效）"),
                            ("export-all", "为所有当前可执行任务导出任务包（零网络）"),
                            ("review-export", "导出主模型审核请求（读图后回写结果）"),
                            ("review-import", "导入主模型审核结果并校验六项绑定"),
                            ("publish", "把已通过的审核结果发布为正式版本"),
                            ("run-ready", "按依赖图批量生成当前可执行任务（需密钥）"),
                            ("resume", "崩溃后从事件日志恢复并继续调度"),
                            ("status", "查看视觉生产状态（--json 给机器读）"),
                            ("pause", "暂停后续派发（--clear 解除）")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--lib", required=True)
        p.add_argument("--task", help="任务 ID")
        if name == "import":
            p.add_argument("--image", required=True, help="手工生成的结果图片")
        if name == "review-import":
            p.add_argument("--result", required=True, help="主模型审核结果 JSON")
        if name == "check":
            p.add_argument("--qc", help="结构化检查结果（JSON）")
        if name in ("run-ready", "resume"):
            p.add_argument("--parallel", type=int, help="并发数 1–4（默认取项目设置）")
        if name == "status":
            p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
        if name == "pause":
            p.add_argument("--clear", action="store_true", help="解除暂停")
    args = parser.parse_args()
    lib = Path(args.lib)
    if args.cmd == "status":
        return cmd_status(lib, as_json=bool(args.json))
    if args.cmd == "pause":
        return cmd_pause(lib, clear=bool(args.clear))
    if args.cmd == "run-ready":
        return cmd_run_ready(lib, args.parallel)
    if args.cmd == "resume":
        return cmd_resume(lib, args.parallel)
    if args.cmd == "compose":
        return cmd_compose(lib, args.task)
    if args.cmd == "export-all":
        return cmd_export_all(lib)
    if args.cmd == "review-export":
        if not args.task:
            print("❌ review-export 需要 --task")
            return 1
        return cmd_review_export(lib, args.task)
    if args.cmd == "review-import":
        if not args.task:
            print("❌ review-import 需要 --task")
            return 1
        return cmd_review_import(lib, args.task, Path(args.result))
    if args.cmd == "publish":
        if not args.task:
            print("❌ publish 需要 --task")
            return 1
        return cmd_publish(lib, args.task)
    if args.cmd == "approve-composition":
        if not args.task:
            print("❌ approve-composition 需要 --task")
            return 1
        return cmd_approve_composition(lib, args.task)
    if args.cmd == "export":
        if not args.task:
            print("❌ export 需要 --task")
            return 1
        return cmd_export(lib, args.task)
    if args.cmd == "import":
        if not args.task:
            print("❌ import 需要 --task")
            return 1
        return cmd_import(lib, args.task, Path(args.image))
    if args.cmd == "check":
        return cmd_check(lib, args.task, Path(args.qc) if args.qc else None)
    if not args.task:
        print("❌ run 需要 --task")
        return 1
    return cmd_run(lib, args.task)


if __name__ == "__main__":
    sys.exit(main())
