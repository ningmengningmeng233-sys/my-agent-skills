"""技能结构守卫：上下文负担回归防线（Task 11 后审计补齐）。

背景：体积审计结论——SKILL.md 10,398 字节（LIGHTWEIGHT 档），主入口与 references 之间
零重复长行，references 两两无大段重复，无外部技能全文副本。本套件把这些审计结论固化为
自动守卫，防止未来改动重新引入上下文负担：

1. 主入口体积带：SKILL.md ≤ 25KB 硬上限（预警指标，与 Task 1 审计口径一致）；
2. 必需章节仍在：§0—§6 与 frontmatter 三要素不得被改名/删除；
3. 主入口不含 CHANGELOG / 历史版本记录（版本历史只允许在 CHANGELOG.md 与 config/version.json）；
4. references 之间无大段重复（两文件共享 ≥12 条 ≥40 字符长行即失败）；
5. 无超 50KB 的单个可加载文本（references/**.md、SKILL.md、AGENTS.md）；
6. 版本声明单一权威来源：`config/version.json` 为唯一事实源，SKILL.md 与 AGENTS.md 的
   版本标注必须与之相等；
7. 所有路由目标存在 + 无整目录加载指令（引用既有 `skill_context_audit` 实测，防回归）。

运行（目录名不是合法 Python 标识符，禁止 `-m unittest` 模块路径）：

    python "minimax-h3-craft/_审查测试/test_skill_structure_guards.py" -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]

HARD_CAP_BYTES = 25 * 1024
LOADABLE_FILE_CAP_BYTES = 50 * 1024
DUPLICATE_LINE_THRESHOLD = 12
DUPLICATE_MIN_LINE_CHARS = 40

REQUIRED_SECTIONS = (
    "# MiniMax H3 工业级提示词工程规范",
    "## 0. 启动引导与交互填空卡协议",
    "## 1. 模型能力判定与生成模式裁决",
    "## 2. 输出协议判定",
    "### 2.1 多片段与整集时长合同",
    "## 3. 按需加载路由",
    "## 4. 核心生成规则",
    "## 5. 输出契约",
    "## 6. 自动校验与命令行门禁",
)


def read_skill() -> str:
    return (SKILL_ROOT / "SKILL.md").read_text("utf-8")


def long_lines(path: Path, minimum: int = DUPLICATE_MIN_LINE_CHARS) -> set:
    lines = set()
    for raw in path.read_text("utf-8").splitlines():
        stripped = raw.strip()
        if len(stripped) >= minimum and not stripped.startswith("|"):
            lines.add(stripped)
    return lines


class MainEntrySizeGuard(unittest.TestCase):
    def test_skill_entry_stays_under_hard_cap(self):
        size = (SKILL_ROOT / "SKILL.md").stat().st_size
        self.assertLessEqual(
            size, HARD_CAP_BYTES,
            f"SKILL.md {size} 字节，超过 25KB 硬上限——必须拆分主入口",
        )

    def test_no_oversized_single_loadable_text(self):
        loadable = [Path("SKILL.md"), Path("AGENTS.md")]
        loadable += sorted(Path("references").rglob("*.md"))
        offenders = [
            (str(p.relative_to(SKILL_ROOT)), p.stat().st_size)
            for p in loadable
            if p.is_file() and p.stat().st_size > LOADABLE_FILE_CAP_BYTES
        ]
        self.assertEqual(offenders, [], f"存在超 50KB 的单个可加载文本：{offenders}")


class RequiredSectionsGuard(unittest.TestCase):
    def test_required_sections_survive_refactors(self):
        skill = read_skill()
        for section in REQUIRED_SECTIONS:
            self.assertIn(section, skill, f"主入口必需章节缺失：{section}")

    def test_frontmatter_keeps_identity_fields(self):
        skill = read_skill()
        for field in ("name:", "version:", "description:"):
            self.assertIn(field, skill.split("---")[1], f"frontmatter 缺少 {field}")


class NoHistoryInMainEntryGuard(unittest.TestCase):
    def test_main_entry_has_no_changelog_or_version_history(self):
        skill = read_skill()
        self.assertNotIn("CHANGELOG.md 内容", skill)
        history_headers = re.findall(r"^#{2,3} v\d+\.\d+", skill, flags=re.MULTILINE)
        self.assertEqual(
            history_headers, [],
            f"主入口不得包含历史版本记录章节：{history_headers}",
        )


class NoReferenceDuplicationGuard(unittest.TestCase):
    def test_no_heavy_line_duplication_between_references(self):
        top_level = sorted(p for p in Path(SKILL_ROOT / "references").glob("*.md"))
        offenders = []
        for i, a in enumerate(top_level):
            lines_a = long_lines(a)
            for b in top_level[i + 1:]:
                shared = lines_a & long_lines(b)
                if len(shared) >= DUPLICATE_LINE_THRESHOLD:
                    offenders.append((a.name, b.name, len(shared)))
        self.assertEqual(
            offenders, [],
            f"references 之间存在大段重复（阈值 {DUPLICATE_LINE_THRESHOLD} 条长行）：{offenders}",
        )

    def test_main_entry_does_not_copy_reference_full_text(self):
        skill_lines = long_lines(SKILL_ROOT / "SKILL.md", minimum=30)
        for p in sorted(Path(SKILL_ROOT / "references").glob("*.md")):
            shared = skill_lines & long_lines(p, minimum=30)
            self.assertLess(
                len(shared), 5,
                f"SKILL.md 与 {p.name} 重复长行 {len(shared)} 条——主入口应只保留短合同",
            )


class VersionSourceGuard(unittest.TestCase):
    def test_version_is_declared_in_exactly_one_authority(self):
        version_doc = (SKILL_ROOT / "config" / "version.json").read_text("utf-8")
        import json

        authoritative = json.loads(version_doc)["version"]
        skill = read_skill()
        self.assertIn(f"version: {authoritative}", skill)
        self.assertIn(f"(v{authoritative})", skill)
        agents = (SKILL_ROOT / "AGENTS.md").read_text("utf-8")
        self.assertIn(f"v{authoritative}", agents)


class RouteIntegrityGuard(unittest.TestCase):
    def test_live_context_audit_stays_green(self):
        """整目录加载指令 / 路由目标缺失 / 体积带越界的实时审计不得回归。"""
        from conftest import run_py

        proc = run_py([str(SKILL_ROOT / "scripts" / "skill_context_audit.py"),
                       str(SKILL_ROOT)])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('"status": "PASS"', proc.stdout)
        self.assertIn('"missing_route_targets": []', proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
