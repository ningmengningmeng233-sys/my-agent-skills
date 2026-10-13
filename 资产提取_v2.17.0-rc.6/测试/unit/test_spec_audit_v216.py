#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 8 工单 8.5：最终规格审计（机械版）。

把「规格条款 → 实现文件 → 测试 → 证据」的矩阵钉成测试，外加三项扫描：

- 占位符与调试残留：产品代码与交付文档命中必须为 0；
- 计划写入点：只有协调器（持锁）与迁移器/入口允许写 `视觉资产/计划.toml`，且协调器写入必须在锁内；
- `GeminiQcChecker` 正式入口不可达（生成器没有签发权）。

真实图片内容审核与第一集迁移 `apply` 不在此文件范围（需能看图的主模型，见验收报告）。
"""
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 规格条款 → (实现文件…, 测试文件…)
SPEC_MATRIX = {
    "三种视觉模式与项目设置": (
        ("scripts/engine/project_settings.py", "scripts/maintenance.py",
         "scripts/visual_production.py"),
        ("测试/unit/test_project_settings.py", "测试/unit/test_visual_modes.py"),
    ),
    "视觉数据契约与六项审核绑定": (
        ("scripts/engine/visual_schema.py", "scripts/engine/review_contract.py"),
        ("测试/unit/test_visual_schema.py", "测试/unit/test_review_contract.py"),
    ),
    "正式版本发布器与交付门交叉验证": (
        ("scripts/engine/visual_publisher.py", "scripts/check_visual_delivery.py"),
        ("测试/unit/test_visual_publisher.py", "测试/unit/test_visual_delivery_gate_rc2.py"),
    ),
    "导演构图卡": (
        ("scripts/engine/director_composition.py", "templates/导演构图卡.toml"),
        ("测试/unit/test_director_composition.py",),
    ),
    "场景站位图与连续性": (
        ("scripts/engine/scene_blocking.py", "templates/场景站位图.toml"),
        ("测试/unit/test_scene_blocking.py",),
    ),
    "构图进提示词编译与禁止项独立字段": (
        ("scripts/engine/visual_request_chain.py", "scripts/engine/image_request_builder.py"),
        ("测试/unit/test_visual_reference_chain.py", "测试/unit/test_image_executor.py"),
    ),
    "生成器注册表与 Codex 转接": (
        ("scripts/engine/generator_registry.py", "scripts/engine/gemini_image.py"),
        ("测试/unit/test_generator_registry.py",),
    ),
    "只提示词模式零网络": (
        ("scripts/visual_production.py", "scripts/engine/image_executor.py"),
        ("测试/unit/test_visual_modes.py", "测试/unit/test_visual_cli.py"),
    ),
    "源头脸与正背立绘规则": (
        ("scripts/engine/visual_planner.py", "templates/视觉质检/人物.toml"),
        ("测试/unit/test_visual_planner.py", "测试/unit/test_prompt_and_costume.py"),
    ),
    "单写者调度器与事件日志": (
        ("scripts/engine/visual_scheduler.py", "scripts/engine/visual_events.py"),
        ("测试/unit/test_visual_scheduler.py",),
    ),
    "批量入口与暂停/恢复命令": (
        ("scripts/visual_production.py",),
        ("测试/unit/test_visual_cli.py",),
    ),
    "传输重试与自适应并发": (
        ("scripts/engine/retry_policy.py", "scripts/engine/visual_scheduler.py"),
        ("测试/unit/test_retry_policy.py",),
    ),
    "主模型审核往返与发布命令": (
        ("scripts/engine/review_provider.py", "scripts/visual_production.py"),
        ("测试/unit/test_review_provider.py",),
    ),
    "结构化硬门与判定集合": (
        ("scripts/engine/image_qc.py", "templates/视觉质检/人物.toml",
         "templates/视觉质检/场景.toml", "templates/视觉质检/道具.toml"),
        ("测试/unit/test_image_qc.py", "测试/unit/test_visual_gold_contract.py"),
    ),
    "可选双审核子代理": (
        ("scripts/engine/review_merge.py", "references/审核子代理-美术.md",
         "references/审核子代理-技术.md"),
        ("测试/unit/test_review_merge.py",),
    ),
    "旧库非破坏迁移": (
        ("scripts/migrate_visual_library.py",),
        ("测试/unit/test_visual_migration_v216.py",),
    ),
    "第一集验收报告（机器渲染）": (
        ("测试/run_visual_migration_acceptance.py",
         "文档/验收/v2.16.0-第一集视觉迁移验收.md"),
        ("测试/unit/test_visual_migration_v216.py",),
    ),
    "主入口瘦身与按需路由": (
        ("SKILL.md", "references/核心反推契约.md", "references/视觉生产契约.md",
         "references/Omni质量审核契约.md", "references/角色参考入库契约.md",
         "references/导演构图与站位契约.md", "references/并发与失败处理契约.md"),
        ("测试/unit/test_skill_router_size.py",),
    ),
    "发布级自动门": (
        ("测试/run_all_checks.py", "scripts/secret_scan.py", "scripts/build_release.py"),
        ("测试/unit/test_release_gate_coverage.py",),
    ),
    "中文长路径与跨平台": (
        ("scripts/engine/project_settings.py",),
        ("测试/unit/test_long_cjk_paths.py", "测试/unit/test_release_gate_coverage.py"),
    ),
}

# 允许写计划的模块（其余一律不得出现写入调用）；协调器写入必须在锁内
PLAN_WRITERS = {
    "scripts/engine/visual_scheduler.py": "协调器：_commit/_checkpoint 在 plan_lock 内提交",
    "scripts/migrate_visual_library.py": "迁移器：单次迁移登记候选",
    "scripts/maintenance.py": "建计划入口（visual-plan）",
    "scripts/visual_production.py": "单任务命令（compose/approve/run/import 等）",
    "scripts/create_visual_plan.py": "建计划入口（旧路径）",
    "scripts/render_visual_plan.py": "渲染阅读稿时同步写回计划",
    "scripts/engine/visual_migration.py": "旧库迁移工具（migrate-visual）",
}
# 定义处自身不算调用方（它是实现，不是写入点）。
# T07 拆分后，实现搬到 visual_io.py，visual_schema 只剩显式 re-export 门面（不再含调用形状）。
PLAN_WRITER_DEFINITION = "scripts/engine/visual_io.py"

PLACEHOLDER_PATTERNS = (
    "TODO", "FIXME", "HACK", "占位待实现", "剩余代码省略", "rest of code",
    "breakpoint(", "pdb.set_trace", 'print("debug', "print('debug",
)
# 说明：不检查裸 XXX —— 文档里对转义规则的说明会误命中，而它并非占位符。
SCAN_ROOTS = ("scripts", "SKILL.md", "references")
SCAN_SUFFIXES = (".py", ".md", ".toml")


# 工程过程文档（交接/设计/工单/验收）不进发布包：发布包只带技能本体。
# 这些路径在源码仓库里必须存在；在包副本（无 .git）里允许缺席。
PROCESS_ONLY_PREFIXES = ("文档/",)


def _must_exist(rel: str) -> bool:
    """该文件在当前位置是否**必须**存在（包副本里豁免工程过程文档）。"""
    in_repo = (REPO_ROOT / ".git").exists()
    if not in_repo and any(rel.startswith(prefix) for prefix in PROCESS_ONLY_PREFIXES):
        return False
    return True


def test_spec_matrix_files_and_tests_exist():
    problems = []
    for clause, (impl, tests) in SPEC_MATRIX.items():
        for rel in impl:
            if _must_exist(rel) and not (REPO_ROOT / rel).is_file():
                problems.append(f"{clause} 缺实现文件 {rel}")
        for rel in tests:
            if _must_exist(rel) and not (REPO_ROOT / rel).is_file():
                problems.append(f"{clause} 缺测试文件 {rel}")
    assert not problems, problems


def test_spec_matrix_tests_contain_real_tests():
    problems = []
    for clause, (_impl, tests) in SPEC_MATRIX.items():
        for rel in tests:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            if not re.search(r"^def test_|^\s+def test_", text, re.M):
                problems.append(f"{clause} 的 {rel} 里没有测试函数")
    assert not problems, problems


def test_no_placeholders_in_product_code_or_delivery_docs():
    """占位符与调试残留：产品代码与交付文档命中必须为 0。"""
    hits = []
    for root in SCAN_ROOTS:
        target = REPO_ROOT / root
        files = [target] if target.is_file() else [
            p for p in target.rglob("*") if p.is_file() and p.suffix in SCAN_SUFFIXES]
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            rel = path.relative_to(REPO_ROOT).as_posix()
            for pattern in PLACEHOLDER_PATTERNS:
                if pattern in text:
                    hits.append(f"{rel}: {pattern}")
    assert not hits, f"发现占位符/调试残留：{hits}"


def test_only_allowed_modules_write_the_plan():
    callers = set()
    for path in (REPO_ROOT / "scripts").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"write_visual_plan\s*\(", text):
            callers.add(path.relative_to(REPO_ROOT).as_posix())
    callers.discard(PLAN_WRITER_DEFINITION)
    unknown = sorted(callers - set(PLAN_WRITERS))
    assert not unknown, f"这些模块也写了计划，未在审计白名单内：{unknown}"


def test_scheduler_writes_plan_inside_the_lock():
    """协调器的计划写入必须在 plan_lock 内（否则并发下会丢更新）。"""
    text = (REPO_ROOT / "scripts" / "engine" / "visual_scheduler.py").read_text(
        encoding="utf-8")
    for func in ("_commit", "_checkpoint"):
        body = text.split(f"def {func}(", 1)[1].split("\n    def ", 1)[0]
        assert "with plan_lock(" in body, f"{func} 未在锁内提交"
        assert "write_visual_plan(" in body, f"{func} 没有落盘调用"


def test_plan_lock_is_a_kernel_lock_with_reentrancy():
    text = (REPO_ROOT / "scripts" / "engine" / "visual_scheduler.py").read_text(
        encoding="utf-8")
    assert "msvcrt.locking" in text and "flock" in text, "计划锁必须是内核级文件锁"
    assert "reentrant" in text or "可重入" in text, "必须说明同线程可重入语义"


def test_gemini_qc_checker_is_unreachable_from_formal_paths():
    """生成器没有签发权：正式入口不得引用 Gemini 终审类。"""
    offenders = []
    for rel in ("scripts/visual_production.py", "scripts/engine/visual_pipeline.py",
                "scripts/engine/visual_scheduler.py", "scripts/engine/review_provider.py"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        if "GeminiQcChecker" in text:
            offenders.append(rel)
    assert not offenders, f"正式路径引用了 Gemini 终审：{offenders}"


def test_hard_gates_and_decisions_match_the_spec():
    from scripts.engine.image_qc import HARD_GATE_CODES, MANDATED_DECISIONS

    assert len(HARD_GATE_CODES) == 16
    assert tuple(MANDATED_DECISIONS) == ("ACCEPTED", "PASS_WITH_NOTES",
                                         "NEEDS_REVISION", "NEEDS_HUMAN_DECISION")


def test_no_xfail_marks_remain():
    """阶段 0 钉的红测必须全部转绿；任何残留 xfail 都算未完成。"""
    hits = []
    for path in (REPO_ROOT / "测试").rglob("*.py"):
        if path.name == Path(__file__).name:
            continue  # 本文件必须提到 xfail 才能检查它
        text = path.read_text(encoding="utf-8", errors="replace")
        if "xfail" in text:
            hits.append(path.relative_to(REPO_ROOT).as_posix())
    assert not hits, f"仍有 xfail 标记：{hits}"


@pytest.mark.parametrize("doc", ("README.md", "QUICKSTART.md", "FAQ.md", "CONTEXT.md"))
def test_docs_do_not_overclaim_verified_status(doc):
    """文档不得声称真实 API / 第一集迁移已完成（无物理证据）。"""
    text = (REPO_ROOT / doc).read_text(encoding="utf-8")
    for claim in ("真实 API 已通过", "第一集已正式交付", "视觉内容已通过人工审核"):
        assert claim not in text, f"{doc} 出现无证据的完成声明：{claim}"
