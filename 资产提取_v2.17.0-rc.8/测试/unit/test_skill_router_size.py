#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 8 工单 8.1：主入口瘦身为按需路由（规格三）。

- `SKILL.md` 只保留职责、触发、三种模式、核心流程、硬规则与加载路由；
  硬上限 25KB，目标 15–20KB（低到不可用的下限为 10KB）。
- 细则落 `references/*.md`；拆分不得丢内容：老主入口的每个章节都必须仍可在
  主入口或 references 里找到。
- 视觉未启用时，主入口不得要求读取视觉类文档（视觉章节必须写明「只有启用时才需要读」）。
"""
import re
import sys
from pathlib import Path

import skill_meta

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

SKILL = REPO_ROOT / "SKILL.md"
REFERENCES = REPO_ROOT / "references"

HARD_LIMIT = 25 * 1024
TARGET_MIN = 15 * 1024
TARGET_MAX = 20 * 1024
USABLE_MIN = 10 * 1024

ROUTER_FILES = (
    "references/核心反推契约.md",
    "references/视觉生产契约.md",
    "references/Omni质量审核契约.md",
    "references/角色参考入库契约.md",
    "references/导演构图与站位契约.md",
    "references/并发与失败处理契约.md",
    "references/错误处理矩阵.md",
)

# 老主入口的章节标题与标志性内容：拆分后必须仍然可达（防「拆没了」）
KEPT_MARKERS = (
    "纯中文输出约定",
    "核心与可选视觉的边界",
    "开始界面",
    "全局登记表",
    "段级提取",
    "聚合裁决",
    "服饰设计",
    "描述补全",
    "基础提示词",
    "衍生提示词",
    "完整性检查",
    "交付契约",
    "追更",
    "错误处理矩阵",
    "数据布局",
    "两条路径",
    "视觉交付门",
    "权威风格包",
    "输入格式",
)


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _all_readable_text() -> str:
    parts = [_skill_text()]
    for path in sorted(REFERENCES.rglob("*.md")):
        parts.append(path.read_text(encoding="utf-8"))
    parts.append((REPO_ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_skill_entry_is_within_size_budget():
    size = len(_skill_text().encode("utf-8"))
    assert size <= HARD_LIMIT, f"主入口 {size} 字节超过硬上限 25KB"
    assert size >= USABLE_MIN, f"主入口只剩 {size} 字节，太薄以致无法执行（下限 10KB）"
    if size < TARGET_MIN:
        # 低于目标区间不算失败，但必须显式记录，避免悄悄瘦成空壳
        assert "按需加载路由" in _skill_text()
        assert len(re.findall(r"references/[^\s`|）]+\.md", _skill_text())) >= 6


def test_skill_keeps_the_six_required_parts():
    text = _skill_text()
    for marker in ("## 一、你是谁", "这次是否直接生成图片", "## 四、核心反推流程概览",
                   "## 五、视觉生产流程概览", "## 六、不可违反的硬规则", "## 七、按需加载路由"):
        assert marker in text, f"主入口缺少必需部分：{marker}"


def test_skill_declares_version_once():
    text = _skill_text()
    versions = {skill_meta.strip_version_prefix(v) for v in skill_meta.prose_versions(text)}
    assert len(versions) == 1, f"主入口出现多个版本号：{sorted(versions)}"
    assert "docs/CHANGELOG.md" in text, "历史版本说明必须外移并给出指针"


def test_router_files_exist_and_are_referenced():
    text = _skill_text()
    for rel in ROUTER_FILES:
        assert (REPO_ROOT / rel).is_file(), f"缺少路由文件：{rel}"
        assert Path(rel).name in text, f"主入口未指向路由文件：{rel}"


def test_visual_docs_are_gated_behind_enabling_visual_production():
    """视觉未启用时不读视觉细则：主入口必须把这件事写明。"""
    text = _skill_text()
    assert "视觉未启用时" in text and "不加载" in text, (
        "主入口必须写明「视觉未启用时不加载视觉细则」")
    visual = (REFERENCES / "视觉生产契约.md").read_text(encoding="utf-8")
    assert "只有启用视觉生产时才需要读本文件" in visual


def test_router_split_kept_all_original_sections_reachable():
    """拆分不得丢内容：老主入口的章节与要点必须仍能在主入口或 references 里找到。"""
    haystack = _all_readable_text()
    missing = [marker for marker in KEPT_MARKERS if marker not in haystack]
    assert not missing, f"拆分后找不到这些原章节内容：{missing}"


def test_changelog_holds_the_history_that_left_the_entry():
    changelog = (REPO_ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "主入口历史摘要" in changelog, "历史版本说明必须落到 CHANGELOG"
    skill = _skill_text()
    assert "v2.14.0-rc.4 候选" not in skill, "历史条目不得留在主入口"
    assert "v2.11.5 补丁" not in skill
