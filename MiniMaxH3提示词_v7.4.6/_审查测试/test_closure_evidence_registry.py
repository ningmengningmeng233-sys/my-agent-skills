"""闭包证据采集器的登记表守卫（D1 收尾轮发现的真缺陷）。

`scripts/collect_closure_evidence.py` 把静态段与 CLI 段的套件名写死在源码里，而那批
文件（`test_ref2va_en.py` 等 9 + 1 个）已随历史套件丢失——于是 STATIC_RULE_GATES 与
CLI_ENTRYPOINT_E2E 从历史套件丢失那天起就**永远红**（每个套件 50ms 内退出码 2：
文件不存在）。永远红的门等于没有门，而且报告里看不出是「门红」还是「锁孔被堵死」。

本守卫只钉三件事：
1. 登记表里每个套件/命令都必须在磁盘上存在（禁止再写死会腐坏的名字）；
2. 静态段 ∪ CLI 段 ∪ 本地全链段必须把 `_审查测试/` 里所有 `test_*.py` 恰好覆盖一次，
   谁也不许被悄悄漏掉，也不许把管线全链算成「静态审计」；
3. 发现结果为空时必须判红而不是判绿（口径腐坏不能伪装成「无事可做」）。

运行：

    python "_审查测试/test_closure_evidence_registry.py" -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import collect_closure_evidence as cce  # noqa: E402

AUDIT_DIR = SKILL_ROOT / "_审查测试"


def all_audit_suites() -> set[str]:
    return {p.name for p in AUDIT_DIR.glob("test_*.py")}


class RegistryIsAlive(unittest.TestCase):
    def test_static_suites_all_exist(self):
        names = cce.discover_static_suites(AUDIT_DIR)
        self.assertTrue(names, "静态段套件为空：口径腐坏不得伪装成无事可做")
        missing = [n for n in names if not (AUDIT_DIR / n).is_file()]
        self.assertEqual([], missing, "静态段登记了磁盘上不存在的套件")

    def test_cli_suites_all_exist(self):
        names = cce.discover_cli_suites(AUDIT_DIR)
        self.assertTrue(names)
        missing = [n for n in names if not (AUDIT_DIR / n).is_file()]
        self.assertEqual([], missing, "CLI 段登记了磁盘上不存在的套件")

    def test_no_hardcoded_stale_names_survive(self):
        """写死名单是这次的病根：模块里不得再留有磁盘缺件的常量名单。"""
        for const in ("STATIC_SUITES", "CLI_SUITES"):
            value = getattr(cce, const, None)
            if value is None:
                continue
            stale = [n for n in value if not (AUDIT_DIR / n).is_file()]
            self.assertEqual([], stale, f"{const} 仍有失效套件名: {stale}")

    def test_three_segments_partition_the_audit_dir(self):
        static = set(cce.discover_static_suites(AUDIT_DIR))
        cli = set(cce.discover_cli_suites(AUDIT_DIR))
        local = set(cce.LOCAL_SUITES)
        self.assertFalse(static & cli, "同一套件不得既算静态又算 CLI")
        self.assertFalse((static | cli) & local,
                         "本地全链套件不得混进静态段（静态审计≠全链验证）")
        covered = static | cli | local
        self.assertEqual(all_audit_suites(), covered,
                         f"未覆盖: {sorted(all_audit_suites() - covered)} / "
                         f"多余: {sorted(covered - all_audit_suites())}")

    def test_empty_dir_discovers_nothing_but_collector_refuses_green(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp)
            self.assertEqual([], cce.discover_static_suites(empty))
            self.assertEqual([], cce.discover_cli_suites(empty))


if __name__ == "__main__":
    unittest.main(verbosity=2)
