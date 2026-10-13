#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T07：visual_schema 兼容解耦的行为闸门（工单 T07 红测）。

拆分的唯一承诺是「行为不变」，所以这里量的是外部可见的一切：
- 门面必须还能导出原有的每一个公开名（含被 64 个生产文件实际导入的 28 个）；
- 函数签名逐字不变；
- 类型必须是**同一个对象**，不允许复制一份同名 dataclass（否则 isinstance 与 repr 会静默改变）；
- 旧计划读写往返保持字段与值；
- 校验错误类别集合不变；
- 原子写失败时原文件保持原样；
- 拆分不得引入新的循环依赖。

本文件在拆分前应当是红的（visual_types/visual_validation/visual_io 还不存在）。
"""
from __future__ import annotations

import dataclasses
import importlib
import inspect
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine import visual_schema as vs  # noqa: E402

# 仓库内唯一带真实任务的计划夹具（gold 目录下的 expected_plan.toml 是契约断言用的
# 局部片段，load 出来 tasks 为空，不能当往返样本）
PLAN_FIXTURE = REPO_ROOT / "templates" / "视觉计划模板.toml"

# 现场录制（拆分前）的公开签名：任何一处变动都必须先改这条尺并说明理由
SIGNATURES = {
    "user_status_label": "(status: 'str') -> 'str'",
    "user_decision_label": "(decision: 'str') -> 'str'",
    "normalize_rel_path": "(value: 'str | None') -> 'str'",
    "is_relative_safe": "(path: 'str') -> 'bool'",
    "validate_visual_plan": "(plan: 'VisualPlan', asset_index: 'dict | None') -> 'list[ValidationIssue]'",
    "render_visual_plan": "(plan: 'VisualPlan') -> 'str'",
    "write_visual_plan": "(plan: 'VisualPlan', path: 'Path') -> 'None'",
    "load_visual_plan": "(path: 'Path') -> 'VisualPlan'",
}

# 64 个生产文件实际导入的全部公开名（AST 采集，含仅被类型标注使用的名字）
EXTERNALLY_IMPORTED = [
    "ASPECT_BY_KIND", "AttemptRecord", "CompositionSpec", "DEPTH_VALUES", "MAX_AUTO_ROUNDS",
    "PropPlacement", "QcDecision", "REVIEW_DECISIONS", "ReferenceBinding",
    "ReferenceRole", "ReviewBinding", "SOURCE_LEVELS", "STORY_TASK_KINDS", "SubjectPlacement",
    "ValidationIssue", "VisualPlan", "VisualTask", "VisualTaskKind", "VisualTaskStatus",
    "ZONE_VALUES", "is_relative_safe", "load_visual_plan", "normalize_rel_path",
    "render_visual_plan", "user_decision_label", "user_status_label", "validate_visual_plan",
    "write_visual_plan",
]

CLASS_FIELDS = {
    "ValidationIssue": 3, "ReferenceBinding": 4, "QcRecord": 5, "SubjectPlacement": 8,
    "PropPlacement": 5, "CompositionSpec": 9, "ReviewBinding": 6, "AttemptRecord": 10,
    "VisualTask": 29, "VisualPlan": 11,
}

ENUM_MEMBERS = {"VisualTaskKind": 12, "VisualTaskStatus": 13, "ReferenceRole": 6,
                "QcDecision": 5}

# 拆分后的预期归属（用来证明「移动」而不是「复制」）
HOUSES = {
    "visual_types": ["VisualTaskKind", "VisualTaskStatus", "ReferenceRole", "QcDecision",
                     "VisualPlan", "VisualTask", "AttemptRecord", "ReferenceBinding", "QcRecord",
                     "SubjectPlacement", "PropPlacement", "CompositionSpec", "ReviewBinding",
                     "MAX_AUTO_ROUNDS", "ASPECT_BY_KIND", "STORY_TASK_KINDS", "REVIEW_DECISIONS",
                     "ZONE_VALUES", "DEPTH_VALUES", "SOURCE_LEVELS", "SCHEMA_VERSION",
                     "USER_STATUS_LABELS", "USER_DECISION_LABELS", "PUBLISHABLE_STATUSES",
                     "PRODUCED_STATUSES"],
    "visual_validation": ["validate_visual_plan", "ValidationIssue"],
    "visual_io": ["render_visual_plan", "write_visual_plan", "load_visual_plan"],
}


def test_facade_still_exports_every_public_name():
    missing = [n for n in EXTERNALLY_IMPORTED if not hasattr(vs, n)]
    assert not missing, f"门面丢失公开名：{missing}"


def test_function_signatures_are_byte_identical():
    drift = {name: str(inspect.signature(getattr(vs, name)))
             for name, want in SIGNATURES.items()
             if str(inspect.signature(getattr(vs, name))) != want}
    assert not drift, f"签名漂移：{drift}"


def test_dataclass_and_enum_shape_unchanged():
    for name, count in CLASS_FIELDS.items():
        assert len(dataclasses.fields(getattr(vs, name))) == count, name
    for name, count in ENUM_MEMBERS.items():
        assert len(list(getattr(vs, name))) == count, name


def test_type_identity_across_split():
    """复制类型＝最阴的回归：同名不同对象，isinstance 与默认工厂会静默变。"""
    types_mod = importlib.import_module("scripts.engine.visual_types")
    validation_mod = importlib.import_module("scripts.engine.visual_validation")
    io_mod = importlib.import_module("scripts.engine.visual_io")
    for name in HOUSES["visual_types"]:
        if not hasattr(types_mod, name):
            continue
        assert getattr(vs, name) is getattr(types_mod, name), f"{name} 不是同一个对象"
    assert vs.VisualTask is types_mod.VisualTask
    assert vs.VisualPlan is types_mod.VisualPlan
    assert vs.validate_visual_plan is validation_mod.validate_visual_plan
    assert vs.ValidationIssue is validation_mod.ValidationIssue
    assert vs.render_visual_plan is io_mod.render_visual_plan
    assert vs.write_visual_plan is io_mod.write_visual_plan
    assert vs.load_visual_plan is io_mod.load_visual_plan


def test_no_copy_of_the_same_class():
    """同一 dataclass 不得在两处各定义一次（用源文件文本取证，不看导入关系）。"""
    for name in CLASS_FIELDS:
        owners = [m for m in ("visual_schema", "visual_types", "visual_validation", "visual_io")
                  if (REPO_ROOT / "scripts" / "engine" / f"{m}.py").is_file()
                  and re_define((REPO_ROOT / "scripts" / "engine" / f"{m}.py"), name)]
        assert len(owners) == 1, f"{name} 在 {owners} 各定义了一次"


def re_define(path: Path, name: str) -> bool:
    needle = f"class {name}"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(needle):
            return True
    return False


def test_gold_fixture_round_trip_keeps_fields_and_values(tmp_path):
    plan = vs.load_visual_plan(PLAN_FIXTURE)
    first = vs.render_visual_plan(plan)
    out = tmp_path / "round_trip.toml"
    vs.write_visual_plan(plan, out)
    again = vs.load_visual_plan(out)
    assert vs.render_visual_plan(again) == first, "读写往返改变了计划内容"
    assert len(again.tasks) == len(plan.tasks) > 0
    for orig, back in zip(plan.sorted_tasks(), again.sorted_tasks()):
        assert (orig.task_id, orig.kind, orig.status, orig.aspect_ratio) == \
               (back.task_id, back.kind, back.status, back.aspect_ratio)
        assert orig.references == back.references
        assert orig.attempts == back.attempts
    assert (plan.style_slug, plan.style_version, plan.style_hash) == \
           (again.style_slug, again.style_version, again.style_hash)


def test_render_is_deterministic_byte_for_byte():
    plan = vs.load_visual_plan(PLAN_FIXTURE)
    assert vs.render_visual_plan(plan) == vs.render_visual_plan(plan)
    assert "\\u" not in vs.render_visual_plan(plan), "中文不得被转义成 \\uXXXX"


def test_validation_issue_categories_are_unchanged():
    t1 = vs.VisualTask(task_id="C01_正脸-v01", asset_id="C01", kind="不存在", status="PLANNED",
                       aspect_ratio="4:3", style_slug="s", style_version="1", style_hash="h",
                       fact_source="", max_rounds=9,
                       depends_on=("C01_正脸-v01", "不存在_道具-v01"),
                       references=(vs.ReferenceBinding(path="../外.png", role="非法角色"),))
    t2 = vs.VisualTask(task_id="C01_正脸-v01", asset_id="C99", kind="正脸", status="GENERATED",
                       aspect_ratio="1:1", style_slug="s", style_version="2", style_hash="x",
                       fact_source="资产索引",
                       references=(vs.ReferenceBinding(path="C:/绝对.png", role="style"),))
    t3 = vs.VisualTask(task_id="C02_正脸-v01", asset_id="C02", kind="正脸", status="PLANNED",
                       aspect_ratio="1:1", style_slug="s", style_version="1", style_hash="h",
                       fact_source="x", depends_on=("C03_正脸-v01",))
    t4 = vs.VisualTask(task_id="C03_正脸-v01", asset_id="C03", kind="正脸", status="PLANNED",
                       aspect_ratio="1:1", style_slug="s", style_version="1", style_hash="h",
                       fact_source="x", depends_on=("C02_正脸-v01",))
    plan = vs.VisualPlan(style_slug="s", style_version="1", style_hash="h",
                         tasks=(t1, t2, t3, t4))
    index = {"assets": [{"asset_id": "C01"}, {"asset_id": "C02"}, {"asset_id": "C03"}]}
    codes = sorted({i.code for i in vs.validate_visual_plan(plan, index)})
    assert codes == ["ABSOLUTE_PATH", "DEPENDENCY_CYCLE", "DUPLICATE_TASK_ID",
                     "FACT_SOURCE_MISSING", "ILLEGAL_REFERENCE_ROLE", "ILLEGAL_TASK_KIND",
                     "MAX_ROUNDS_OVERFLOW", "PATH_OUTSIDE_LIB", "SELF_DEPENDENCY",
                     "STYLE_PIN_MISMATCH", "TASK_ID_FORMAT", "UNKNOWN_ASSET",
                     "UNKNOWN_DEPENDENCY"], codes
    issue = next(i for i in vs.validate_visual_plan(plan, index)
                 if i.code == "DEPENDENCY_CYCLE")
    assert str(issue) == ("[DEPENDENCY_CYCLE] C02_正脸-v01: 依赖环："
                          "C02_正脸-v01 → C03_正脸-v01 → C02_正脸-v01")


def test_clean_plan_still_valid():
    plan = vs.load_visual_plan(PLAN_FIXTURE)
    index = {"assets": [{"asset_id": t.asset_id} for t in plan.tasks]}
    assert vs.validate_visual_plan(plan, index) == []


def test_atomic_write_failure_keeps_original_file(tmp_path, monkeypatch):
    """写失败不许把原计划清空：临时文件写坏或 replace 失败时原字节必须还在。"""
    target = tmp_path / "计划.toml"
    plan = vs.load_visual_plan(PLAN_FIXTURE)
    vs.write_visual_plan(plan, target)
    before = target.read_bytes()

    for mod_name in ("scripts.engine.visual_io", "scripts.engine.visual_schema"):
        mod = importlib.import_module(mod_name)
        monkeypatch.setattr(mod, "os", _BoomOS(), raising=False)
    with pytest.raises(OSError):
        vs.write_visual_plan(plan, target)
    assert target.read_bytes() == before, "原子写失败后原计划被破坏"


class _BoomOS:
    """只替换 replace：模拟 os.replace 失败，其余行为不动。"""

    @staticmethod
    def replace(src, dst):
        raise OSError("注入：目标被占用")


def test_missing_plan_raises_not_empty_plan(tmp_path):
    with pytest.raises(FileNotFoundError):
        vs.load_visual_plan(tmp_path / "不存在.toml")


def test_no_new_import_cycle_after_split():
    """types 不看任何人；validation/io 只看 types；门面看三者且自身不含逻辑。"""
    engine = REPO_ROOT / "scripts" / "engine"
    assert _deps_of(engine / "visual_types.py") == set(), _deps_of(engine / "visual_types.py")
    assert _deps_of(engine / "visual_validation.py") == {"visual_types"}
    assert _deps_of(engine / "visual_io.py") == {"visual_types"}
    assert _deps_of(engine / "visual_schema.py") == {"visual_types", "visual_validation",
                                                     "visual_io"}
    body = (engine / "visual_schema.py").read_text(encoding="utf-8")
    assert "def " not in body and "class " not in body, "门面里不该再有实现"


def _deps_of(path):
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    deps = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            targets = [node.module or ""]
        elif isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        else:
            continue
        for name in targets:
            tail = name.split(".")[-1]
            if tail.startswith("visual_"):
                deps.add(tail)
    return deps


def test_all_production_importers_still_resolve():
    """64 个生产导入点必须全部还能 import；抽一个最重的入口做真实导入。
    完整清单由 AST 采集脚本比对，这里挡住『门面少给名字导致生产崩』的最低线。"""
    for rel in ("scripts/create_visual_plan.py", "scripts/render_visual_plan.py",
                "scripts/visual_production.py", "scripts/engine/visual_pipeline.py",
                "scripts/engine/visual_migration.py"):
        assert (REPO_ROOT / rel).is_file(), rel
    importlib.import_module("scripts.engine.visual_pipeline")
    importlib.import_module("scripts.engine.visual_migration")


def test_no_phantom_names_in_intra_package_imports():
    """生产代码里 `from .x import 名字` 必须真能取到——函数体内的延迟导入最容易骗过语法。
    实测抓到 1 处：gemini_image.check() 从 visual_schema 取 QcResult（真身在 image_qc）。"""
    import ast
    import importlib

    offenders = []
    engine = REPO_ROOT / "scripts" / "engine"
    for path in sorted(engine.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
                continue
            try:
                mod = importlib.import_module("scripts.engine." + node.module)
            except Exception as exc:  # noqa: BLE001
                offenders.append(f"{path.name}:{node.lineno} 模块导入失败 {type(exc).__name__}")
                continue
            for alias in node.names:
                if alias.name != "*" and not hasattr(mod, alias.name):
                    offenders.append(f"{path.name}:{node.lineno} 向 .{node.module} 取不到 "
                                     f"{alias.name}")
    assert not offenders, offenders
