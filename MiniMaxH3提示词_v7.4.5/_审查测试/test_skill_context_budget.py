"""Task 1 guard：主入口体积、加载路由与整目录加载违规。"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from skill_context_audit import (  # noqa: E402
    audit_skill_context,
    detect_unconditional_load_all,
    main,
)


class RealSkillEntryTest(unittest.TestCase):
    def test_real_skill_entry_is_lightweight(self):
        report = audit_skill_context(SKILL_ROOT)
        self.assertLessEqual(report["skill_bytes"], 25_000)
        self.assertIn(report["target_band_status"], {"LIGHTWEIGHT", "TARGET"})
        self.assertEqual(report["missing_route_targets"], [])
        self.assertFalse(report["unconditional_load_all_references"])
        self.assertEqual(report["status"], "PASS")

    def test_real_skill_code_block_metrics_are_consistent(self):
        """松一致性：不锁死 3/1558，避免后续任务改写 SKILL.md 正文后失效。"""
        report = audit_skill_context(SKILL_ROOT)
        self.assertGreaterEqual(report["code_block_count"], 1)
        self.assertGreater(report["code_block_bytes"], 0)
        self.assertLessEqual(report["code_block_bytes"], report["skill_bytes"])


class SyntheticEntryTest(unittest.TestCase):
    def test_oversized_entry_over_hard_cap_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text("x" * 25_001, encoding="utf-8")
            report = audit_skill_context(root)
        self.assertEqual(report["target_band_status"], "LARGE")
        self.assertIn("SKILL_ENTRY_OVERSIZED", report["hard_failures"])
        self.assertEqual(report["status"], "FAIL")

    def test_entry_exactly_at_hard_cap_passes_with_near_limit_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text("x" * 25_000, encoding="utf-8")
            report = audit_skill_context(root)
        self.assertEqual(report["target_band_status"], "TARGET")
        self.assertTrue(report["near_limit_warning"])
        self.assertEqual(report["status"], "PASS")

    def test_load_all_references_directive_is_hard_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "任务开始时读取 references 下全部文件", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertTrue(report["unconditional_load_all_references"])
        self.assertIn("UNCONDITIONAL_LOAD_ALL_REFERENCES", report["hard_failures"])
        self.assertEqual(report["status"], "FAIL")

    def test_negated_load_all_directive_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "一次不加载整个references目录，按需路由", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertFalse(report["unconditional_load_all_references"])
        self.assertEqual(report["hard_failures"], [])
        self.assertEqual(report["status"], "PASS")

    def test_missing_route_target_is_hard_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text("见 `references/ghost.md`", encoding="utf-8")
            report = audit_skill_context(root)
        self.assertEqual(report["missing_route_targets"], ["references/ghost.md"])
        self.assertIn("MISSING_ROUTE_TARGETS", report["hard_failures"])
        self.assertEqual(report["status"], "FAIL")

    def test_missing_evidence_path_only_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text("见 `e2e/run_agent_e2e.py`", encoding="utf-8")
            report = audit_skill_context(root)
        self.assertEqual(report["missing_route_targets"], [])
        self.assertIn("e2e/run_agent_e2e.py", report["missing_evidence_paths"])
        self.assertEqual(report["status"], "PASS")

    def test_glob_route_target_resolves_against_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "a.json").write_text("{}", encoding="utf-8")
            (root / "SKILL.md").write_text("见 `config/*.json`", encoding="utf-8")
            report = audit_skill_context(root)
        self.assertEqual(report["missing_route_targets"], [])
        self.assertEqual(report["route_targets"], {"config/*.json": True})

    def test_trailing_star_glob_keeps_glob_semantics(self):
        """`config/*` 是目录 glob，剥尾标点时不得塌缩成 `config/` 目录存在性检查。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "a.json").write_text("{}", encoding="utf-8")
            (root / "SKILL.md").write_text("见 `config/*`", encoding="utf-8")
            report = audit_skill_context(root)
        self.assertEqual(report["route_targets"], {"config/*": True})
        self.assertEqual(report["status"], "PASS")

    def test_missing_skill_entry_is_hard_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = audit_skill_context(Path(tmp))
        self.assertEqual(report["skill_bytes"], 0)
        self.assertEqual(report["target_band_status"], "MISSING")
        self.assertIn("MISSING_SKILL_ENTRY", report["hard_failures"])
        self.assertEqual(report["status"], "FAIL")

    def test_load_all_after_clause_boundary_is_still_a_violation(self):
        """否定词在前一子句时不得豁免后一子句的整目录加载指令。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "不要漏掉，读取全部references", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertTrue(report["unconditional_load_all_references"])
        self.assertIn("UNCONDITIONAL_LOAD_ALL_REFERENCES", report["hard_failures"])
        self.assertEqual(report["status"], "FAIL")

    def test_negation_inside_match_span_suppresses_violation(self):
        """否定词落在 match 跨度内部时同样必须被识别为禁止性指令。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "按需加载：不要一次性读取全部references", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertFalse(report["unconditional_load_all_references"])
        self.assertEqual(report["load_all_matches"], [])
        self.assertEqual(report["status"], "PASS")

    def test_uncertain_compound_is_not_treated_as_negation(self):
        """"不确定"是复合词而非否定指令，不得豁免整目录加载。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "不确定时读取全部references", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertTrue(report["unconditional_load_all_references"])
        self.assertIn("UNCONDITIONAL_LOAD_ALL_REFERENCES", report["hard_failures"])
        self.assertEqual(report["status"], "FAIL")

    def test_cjk_glued_route_without_separator_is_detected(self):
        """中文正文直接粘住路由 token（无空格无引号）也必须被扫描到。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "详见references/ghost.md", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertEqual(report["missing_route_targets"], ["references/ghost.md"])
        self.assertIn("MISSING_ROUTE_TARGETS", report["hard_failures"])
        self.assertEqual(report["status"], "FAIL")

    def test_long_cjk_prose_run_does_not_break_route_resolution(self):
        """尾部中文散文串再长也不得因回退上限而误报缺失。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "references").mkdir()
            (root / "references" / "real.md").write_text("# real", encoding="utf-8")
            (root / "SKILL.md").write_text(
                "见 references/real.md" + "这" * 12, encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertEqual(report["missing_route_targets"], [])
        self.assertEqual(report["status"], "PASS")

    def test_markdown_emphasis_wrapped_route_is_detected(self):
        """加粗包裹的路由 token 必须剥成干净路径而不被放过。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "缺 **references/ghost.md**", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertEqual(report["missing_route_targets"], ["references/ghost.md"])
        self.assertIn("MISSING_ROUTE_TARGETS", report["hard_failures"])
        self.assertEqual(report["status"], "FAIL")

    def test_evidence_path_under_review_dir_only_warns(self):
        """_审查测试/ 属证据基础设施前缀，缺失只警告不阻断。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "见 `_审查测试/run_all.py`", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertEqual(report["missing_route_targets"], [])
        self.assertEqual(report["missing_evidence_paths"], ["_审查测试/run_all.py"])
        self.assertEqual(report["status"], "PASS")

    def test_oversized_band_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text("x" * 50_001, encoding="utf-8")
            report = audit_skill_context(root)
        self.assertEqual(report["target_band_status"], "OVERSIZED")
        self.assertIn("SKILL_ENTRY_OVERSIZED", report["hard_failures"])
        self.assertEqual(report["status"], "FAIL")

    def test_near_limit_warning_boundary_at_exactly_twenty_thousand(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text("x" * 20_000, encoding="utf-8")
            report = audit_skill_context(root)
        self.assertFalse(report["near_limit_warning"])
        self.assertEqual(report["target_band_status"], "TARGET")
        self.assertEqual(report["status"], "PASS")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text("x" * 20_001, encoding="utf-8")
            report = audit_skill_context(root)
        self.assertTrue(report["near_limit_warning"])
        self.assertEqual(report["status"], "PASS")

    def test_invalid_utf8_entry_fails_closed_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_bytes(b"\xff\xfe\x00")
            report = audit_skill_context(root)
        self.assertEqual(report["hard_failures"], ["MISSING_SKILL_ENTRY"])
        self.assertEqual(report["target_band_status"], "MISSING")
        self.assertEqual(report["status"], "FAIL")

    def test_colon_label_does_not_hide_load_all_directive(self):
        """冒号标签不得掩盖其后的整目录加载指令。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "一次性加载：references 全部卡片", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertTrue(report["unconditional_load_all_references"])
        self.assertIn("UNCONDITIONAL_LOAD_ALL_REFERENCES", report["hard_failures"])
        self.assertEqual(report["status"], "FAIL")

    def test_core_word_label_does_not_hide_load_all_directive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "整目录加载：references 下全部文件", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertTrue(report["unconditional_load_all_references"])
        self.assertEqual(report["status"], "FAIL")

    def test_reference_dir_then_colon_before_core_is_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "读取 references 目录：全部卡片与示例", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertTrue(report["unconditional_load_all_references"])
        self.assertEqual(report["status"], "FAIL")

    def test_plain_load_all_without_negation_is_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "一次性读取全部references", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertTrue(report["unconditional_load_all_references"])
        self.assertEqual(report["status"], "FAIL")

    def test_irrelevant_uncertainty_prefix_does_not_excuse_load_all(self):
        """"不管…"是无关表述，不是否定指令，不得豁免整目录加载。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "不管什么情况都读取全部references", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertTrue(report["unconditional_load_all_references"])
        self.assertEqual(report["status"], "FAIL")

    def test_single_char_verb_load_all_is_violation(self):
        """"读"这类单字动词同样计入整目录加载。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "有疑问就读全部references", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertTrue(report["unconditional_load_all_references"])
        self.assertEqual(report["status"], "FAIL")

    def test_negation_eleven_chars_before_verb_is_suppressed(self):
        """否定词距动词 11 字，仍须落在子句前窗内被识别。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "不要在任何情况下一次性读取全部references", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertFalse(report["unconditional_load_all_references"])
        self.assertEqual(report["status"], "PASS")

    def test_prohibition_verb_before_load_all_is_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "禁止一次性读取 references 下全部文件", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertFalse(report["unconditional_load_all_references"])
        self.assertEqual(report["status"], "PASS")

    def _audit_with_route_file(self, sentence: str, route_path: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / route_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# card", encoding="utf-8")
            (root / "SKILL.md").write_text(sentence, encoding="utf-8")
            return audit_skill_context(root)

    def test_pointer_all_modes_is_not_load_all_directive(self):
        """核心词前置的指向句（正常按需路由）不得被误判为整目录加载。"""
        report = self._audit_with_route_file(
            "全部可用模式请查看 references/native/model-capabilities.md",
            "references/native/model-capabilities.md",
        )
        self.assertFalse(report["unconditional_load_all_references"])
        self.assertEqual(report["missing_route_targets"], [])
        self.assertEqual(report["status"], "PASS")

    def test_pointer_all_duration_tiers_is_not_load_all_directive(self):
        report = self._audit_with_route_file(
            "所有时长档位请阅读 references/production-discipline.md",
            "references/production-discipline.md",
        )
        self.assertFalse(report["unconditional_load_all_references"])
        self.assertEqual(report["missing_route_targets"], [])
        self.assertEqual(report["status"], "PASS")

    def test_pointer_all_high_risk_shots_is_not_load_all_directive(self):
        report = self._audit_with_route_file(
            "全部高风险镜头请查看 references/high-risk-shots.md",
            "references/high-risk-shots.md",
        )
        self.assertFalse(report["unconditional_load_all_references"])
        self.assertEqual(report["missing_route_targets"], [])
        self.assertEqual(report["status"], "PASS")

    def test_pointer_with_core_word_after_path_is_not_load_all(self):
        """核心词落在文件路径之后的指向句不得被误判。"""
        report = self._audit_with_route_file(
            "查看 references/x.md 的全部时长档位", "references/x.md"
        )
        self.assertFalse(report["unconditional_load_all_references"])
        self.assertEqual(report["missing_route_targets"], [])
        self.assertEqual(report["status"], "PASS")

    def test_verb_first_pointer_is_not_load_all(self):
        """动词前置、核心词后置的指向句不得被误判。"""
        report = self._audit_with_route_file(
            "按需读取 references/duration-ledger.md 获取全部时长规则",
            "references/duration-ledger.md",
        )
        self.assertFalse(report["unconditional_load_all_references"])
        self.assertEqual(report["missing_route_targets"], [])
        self.assertEqual(report["status"], "PASS")

    def test_cjk_suffixed_path_resolves_via_char_rollback(self):
        """真实目录 `文档/评审记录` 被写成 `文档/评审记录目录` 时须逐字回退解出。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "文档" / "评审记录").mkdir(parents=True)
            (root / "SKILL.md").write_text(
                "见 文档/评审记录目录", encoding="utf-8"
            )
            report = audit_skill_context(root)
        self.assertEqual(report["route_targets"], {"文档/评审记录目录": True})
        self.assertEqual(report["missing_route_targets"], [])
        self.assertEqual(report["status"], "PASS")

    def test_long_same_clause_prohibition_is_suppressed(self):
        """否定前窗无长度上限：同子句内长限定语的禁止句不得误报。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "禁止在用户未明确指定卡片范围的情况下一次性读取全部references",
                encoding="utf-8",
            )
            report = audit_skill_context(root)
        self.assertFalse(report["unconditional_load_all_references"])
        self.assertEqual(report["load_all_matches"], [])
        self.assertEqual(report["status"], "PASS")

    def test_long_same_clause_negative_qualifier_is_suppressed(self):
        """否定词距动词远超 16 字但同子句时仍须抑制。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "不要在用户没有明确要求的情况下随意读取全部references",
                encoding="utf-8",
            )
            report = audit_skill_context(root)
        self.assertFalse(report["unconditional_load_all_references"])
        self.assertEqual(report["status"], "PASS")

    def test_core_verb_reference_word_order_is_detected(self):
        """第三种词序 core…verb…ref 必须被锁住，否则删掉 pattern[2] 也不会被发现。"""
        sentence = "全部读取 references 下文件"
        violation, matches = detect_unconditional_load_all(sentence)
        self.assertIs(violation, True)
        self.assertEqual(matches, [{"line": 1, "text": sentence}])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(sentence, encoding="utf-8")
            report = audit_skill_context(root)
        self.assertTrue(report["unconditional_load_all_references"])
        self.assertEqual(report["status"], "FAIL")

    def test_reference_core_verb_word_order_is_documented_as_uncovered(self):
        """第四种词序 ref…core…verb 是 docstring 明示的不覆盖取舍，非缺陷。"""
        self.assertIs(
            detect_unconditional_load_all("references 下全部文件都要读")[0], False
        )
        self.assertIs(
            detect_unconditional_load_all("references 下全部卡片按需加载")[0], False
        )

    def test_commonmark_nested_fence_metrics(self):
        """合成夹具：4 反引号外层块包 3 反引号内层，naive 配对会数出 4 个块。

        夹具为 7 行：标题 / ````markdown / ```text / 内容行 / ``` / ```` / 尾行。
        外层 4 反引号围栏只能被 >=4 反引号的围栏关闭，故 ```` ```text ```` 与
        ```` ``` ```` 都是块内内容，正确答案是 1 个块：
          code_block_bytes = 7 (```text) + 9 (内容行) + 3 (```) = 19。
        skill_bytes 有意不断言，因为它随平台换行约定（LF/CRLF）变化。
        """
        text = (
            "标题\n"
            "````markdown\n"
            "```text\n"
            "内容行\n"
            "```\n"
            "````\n"
            "尾行\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(text, encoding="utf-8")
            report = audit_skill_context(root)
        self.assertEqual(report["skill_lines"], 7)
        self.assertEqual(report["code_block_count"], 1)
        self.assertEqual(report["code_block_bytes"], 19)


class CliContractTest(unittest.TestCase):
    def _invoke_main(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(argv)
        return exit_code, buffer.getvalue()

    def test_cli_passes_real_skill_root_and_emits_json(self):
        exit_code, output = self._invoke_main([str(SKILL_ROOT)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output)["status"], "PASS")

    def test_cli_fails_on_entry_without_skill_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, output = self._invoke_main([tmp])
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output)["status"], "FAIL")

    def test_cli_fails_on_nonexistent_root_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_root = str(Path(tmp) / "does-not-exist")
            exit_code, output = self._invoke_main([missing_root])
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            json.loads(output)["hard_failures"], ["MISSING_SKILL_ENTRY"]
        )


if __name__ == "__main__":
    unittest.main()
