"""M2 批次 6（MM-01/03/06）：闭环证据采集与发布构建诚实性守卫。

只钉三件事（全部 mock/stub，零外部调用）：
1. MM-03：外部校验器文件缺失时如实记为工具缺件（MISSING_VALIDATOR /
   NOT_RUN + 缺件原因），不许虚构成业务通过或伪造业务等待原因；
2. MM-06：build_release 的 git 预检与未入库套件扫描显式锚定当前技能包根，
   monorepo 风格的根相对路径必须去前缀、包外路径必须忽略；
3. MM-01：单一真源版本号校验守卫，包内声明必须与 config/version.json 对齐。

运行：
    python "_审查测试/test_mm_batch6_closure_release.py" -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import build_release as br  # noqa: E402
import collect_closure_evidence as cce  # noqa: E402
import versioning  # noqa: E402

PKG_DIR = SKILL_ROOT.name  # e.g. MiniMaxH3提示词_v7.4.5

BUSINESS_PHRASES = (
    "awaiting native",
    "dry-run",
    "no video packages",
    "no live tasks",
)


class Mm03MissingValidator(unittest.TestCase):
    def test_missing_validator_is_recorded_as_tool_gap(self):
        gate = cce.missing_validator_gate(
            "REAL_AGENT_E2E",
            "e2e/real_agent/verify_transcripts.py",
            "2026-10-13T00:00:00+00:00",
        )
        self.assertEqual(gate["status"], "NOT_RUN")
        self.assertEqual(gate.get("validator_status"), "MISSING_VALIDATOR")
        reason = gate.get("reason", "")
        self.assertIn("e2e/real_agent/verify_transcripts.py", reason)
        lowered = reason.lower()
        for phrase in BUSINESS_PHRASES:
            self.assertNotIn(phrase, lowered,
                             f"缺件原因不得伪装成业务等待：{reason!r}")
        self.assertEqual(gate.get("evidence_paths"), [])

    def test_missing_validator_never_passes(self):
        for name, rel in (
            ("REAL_AGENT_E2E", "e2e/real_agent/verify_transcripts.py"),
            ("MODEL_RENDER_E2E", "e2e/model_render/verify_render_evidence.py"),
            ("VIDEO_REVERSE_E2E", "e2e/video_reverse/verify_video_evidence.py"),
        ):
            gate = cce.missing_validator_gate(name, rel, "now")
            self.assertNotEqual(gate["status"], "PASS",
                                f"{name} 缺校验器时不许 PASS")


class Mm06GitAnchoring(unittest.TestCase):
    def _stub_run_git(self, status_text: str):
        saved = br.run_git

        def fake(args):
            if args[:2] == ["status", "--porcelain"]:
                return status_text
            return saved(args)

        br.run_git = fake
        self.addCleanup(setattr, br, "run_git", saved)

    def test_dirty_paths_are_stripped_and_scoped_to_package(self):
        self._stub_run_git(
            f" M {PKG_DIR}/scripts/x.py\n"
            " M 抖音转录_v2.0.5/scripts/y.py\n"
        )
        self.assertEqual(br.dirty_tracked_paths(), ["scripts/x.py"])

    def test_root_manifest_exempt_survives_monorepo_prefix(self):
        self._stub_run_git(f" M {PKG_DIR}/release-manifest.json\n")
        self.assertEqual(br.dirty_tracked_paths(), [])

    def test_untracked_suites_found_despite_prefix_outside_ignored(self):
        self._stub_run_git(
            f"?? {PKG_DIR}/_审查测试/test_new_suite.py\n"
            "?? OtherPkg/_审查测试/test_other.py\n"
        )
        self.assertEqual(br.untracked_test_suites(),
                         ["_审查测试/test_new_suite.py"])


class Mm01SingleSourceVersion(unittest.TestCase):
    def test_real_package_declarations_align(self):
        self.assertEqual(versioning.check_single_source_version(str(SKILL_ROOT)),
                         "7.4.5")

    def test_drift_is_rejected_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "MiniMaxH3提示词_v9.9.9"
            (root / "config").mkdir(parents=True)
            (root / "config" / "version.json").write_text(
                '{"version": "9.9.9", "release_name": "x"}', encoding="utf-8")
            (root / "SKILL.md").write_text(
                "---\nname: t\nversion: 1.2.3\n---\n# t (v1.2.3)\n",
                encoding="utf-8")
            with self.assertRaises(versioning.VersionValidationError):
                versioning.check_single_source_version(str(root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
