"""发布构建器 `scripts/build_release.py` 的守卫测试（静态段套件）。

只钉构建器自己的诚信，不真跑构建（本文件会被采集器的静态段在执行发布证据
采集时运行，必须纯净、快速、零副作用——全部用合成报告 + 临时目录）：

  1. gate_statuses/gate_reasons 的纯映射：六门键序、非 PASS 必带原因；
  2. build_manifest 装配：清单永不自我封存、file_count 自洽、外部三门如实
     NOT_RUN 不能被装配层偷偷抬成 PASS；
  3. decide_release 拒绝路径：commit 漂移 / 本地门缺证据 / PASS 门没有索引
     文件背书 / 外部门伪造 PASS（缺 per-case 原件）/ 索引字节被改——全判否；
  4. 登记表不腐坏：DIRTY_EXEMPT 只许是根清单，CRITICAL_TRACKED 必须覆盖
     发布链路（含本构建器与本测试自己）。

运行：

    python "_审查测试/test_build_release.py" -v
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import build_release as br  # noqa: E402
from closure_gate_contract import GATE_ORDER  # noqa: E402

PASS_ALL_LOCAL = {
    "STATIC_RULE_GATES": "PASS",
    "CLI_ENTRYPOINT_E2E": "PASS",
    "LOCAL_PIPELINE_E2E": "PASS",
    "REAL_AGENT_E2E": "NOT_RUN",
    "MODEL_RENDER_E2E": "NOT_RUN",
    "VIDEO_REVERSE_E2E": "NOT_RUN",
}


def make_report(statuses, run_id="RUN1", commit="COMMIT1"):
    return {
        "run_id": run_id,
        "commit": commit,
        "evidence_index_sha256": "a" * 64,
        "gates": {
            name: {"status": statuses[name]} for name in GATE_ORDER
        },
    }


def write_backing(run_dir: Path, gate_names):
    """为每个 PASS 门造一个真实存在的索引文件（sha 按字节算）。"""
    index = []
    for gate in gate_names:
        log = run_dir / "logs"
        log.mkdir(exist_ok=True)
        f = log / f"{gate}.log"
        f.write_bytes(b"evidence bytes\n")
        index.append({"path": f"logs/{gate}.log", "type": gate, "kind": "file",
                      "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
                      "bytes": f.stat().st_size})
    return index


class GateMapping(unittest.TestCase):
    def test_gate_statuses_keys_and_order(self):
        report = make_report(PASS_ALL_LOCAL)
        got = br.gate_statuses(report)
        self.assertEqual(list(got), list(GATE_ORDER))
        self.assertEqual(got["LOCAL_PIPELINE_E2E"], "PASS")

    def test_non_pass_gates_always_carry_a_reason(self):
        report = make_report(PASS_ALL_LOCAL)
        reasons = br.gate_reasons(report)
        self.assertEqual(sorted(reasons), sorted(
            ["REAL_AGENT_E2E", "MODEL_RENDER_E2E", "VIDEO_REVERSE_E2E"]))
        self.assertTrue(all(r.strip() for r in reasons.values()),
                        "非 PASS 门的原因不许为空——空原因=沉默放行")

    def test_dirty_exempt_is_only_the_root_manifest(self):
        self.assertEqual(br.DIRTY_EXEMPT, ("release-manifest.json",),
                         "放宽豁免名单等于给构建期偷改文件开门")

    def test_release_chain_files_are_pinned(self):
        for p in ("scripts/build_release.py", "scripts/run_local_batch.py",
                  "scripts/collect_closure_evidence.py",
                  "scripts/closure_gate_contract.py",
                  "_审查测试/test_build_release.py"):
            self.assertIn(p, br.CRITICAL_TRACKED, f"发布链路 {p} 不在必读清单")

    def test_storyboard_consumer_is_pinned_as_runtime(self):
        # W05 的分镜→H3 消费件是运行时代码，不是可选测试：
        # 少了它，包内六列表无人解析，能力声明就是假的。
        for p in ("scripts/storyboard_reader.py", "_审查测试/test_storyboard_reader.py"):
            self.assertIn(p, br.CRITICAL_TRACKED, f"必需运行件 {p} 未进必读清单")

    def _tracked_without(self, drop: str) -> list[str]:
        """合成一份「除 drop 外全部入库」的 tracked 清单，不依赖 .git 是否存在。"""
        base = [p for p in br.CRITICAL_TRACKED if p != drop]
        base += [f"e2e/cases/case{i:02d}.json" for i in range(br.EXPECTED_CORPUS_COUNT)]
        base += [f"e2e/expected/exp{i:02d}.json" for i in range(br.EXPECTED_CORPUS_COUNT)]
        return base

    def _stub_git(self, monkey_attr_values: dict) -> dict:
        saved = {}
        for name, value in monkey_attr_values.items():
            saved[name] = getattr(br, name)
            setattr(br, name, value)
        return saved

    def test_preflight_refuses_missing_required_runtime(self):
        saved = self._stub_git({
            "ls_tracked": lambda: self._tracked_without("scripts/storyboard_reader.py"),
            "dirty_tracked_paths": lambda: [],
            "untracked_test_suites": lambda: [],
            "head_commit": lambda: "h" * 40,
            "current_branch": lambda: "main",
        })
        try:
            with self.assertRaises(SystemExit):
                br.preflight()
        finally:
            for name, value in saved.items():
                setattr(br, name, value)

    def test_preflight_accepts_complete_runtime_set(self):
        # 正对照：必需件齐备时这条闸不得误杀，且发布包在无 .git 的收件人机上也能跑。
        saved = self._stub_git({
            "ls_tracked": lambda: self._tracked_without("__no_such_file__"),
            "dirty_tracked_paths": lambda: [],
            "untracked_test_suites": lambda: [],
            "head_commit": lambda: "h" * 40,
            "current_branch": lambda: "main",
        })
        try:
            stage = br.preflight()
        finally:
            for name, value in saved.items():
                setattr(br, name, value)
        self.assertEqual(stage["head"], "h" * 40)

    def test_runtime_logs_are_withheld_from_release_surface(self):
        # 治理排除层禁发 *.log（pre-commit banned-path 实测拦下过）：构建器必须
        # 在装包侧就遵守，而不是把注定被闸门拒绝的文件塞进发布面。
        self.assertTrue(br.is_withheld("evidence/R1/logs/static-test_x.py.log"))
        self.assertTrue(br.is_withheld("docs/A.LOG"))
        self.assertFalse(br.is_withheld("evidence/R1/evidence-index.json"))
        self.assertFalse(br.is_withheld("release-manifest.json"))
        self.assertEqual(br.WITHHELD_SUFFIXES, (".log",),
                         "禁发名单扩容必须显式改常量并被本测试点名")


class ManifestAssembly(unittest.TestCase):
    def _manifest(self, files_map=None):
        report = make_report(PASS_ALL_LOCAL)
        report["_closure_report_sha256"] = "b" * 64
        files = files_map or {"SKILL.md": {"sha256": "c" * 64, "size_bytes": 10}}
        return br.build_manifest(version="9.9.9", head="h" * 40, branch="main",
                                 timestamp="T", report=report, batch_run_id="B1",
                                 files_map=files,
                                 reasons=br.gate_reasons(report), extra={})

    def test_manifest_binds_this_run_and_never_self_hashes(self):
        m = self._manifest()
        self.assertEqual(m["release_version"], "9.9.9")
        self.assertEqual(m["git_commit"], "h" * 40)
        self.assertEqual(m["evidence_run_id"], "RUN1")
        self.assertEqual(m["closure_report_sha256"], "b" * 64)
        self.assertEqual(m["release_tier"], "local-stable")
        self.assertNotIn("release-manifest.json", m["files"],
                         "清单自我封存会造成哈希悖论")
        self.assertEqual(m["file_count"], len(m["files"]))

    def test_external_not_run_survives_as_not_run(self):
        m = self._manifest()
        for g in ("REAL_AGENT_E2E", "MODEL_RENDER_E2E", "VIDEO_REVERSE_E2E"):
            self.assertEqual(m["gate_statuses"][g], "NOT_RUN",
                             "装配层不许把 NOT_RUN 抬成 PASS")
            self.assertIn(g, m["gate_reasons"])

    def test_files_map_is_sorted(self):
        files = {"b.md": {"sha256": "0" * 64, "size_bytes": 1},
                 "a.md": {"sha256": "1" * 64, "size_bytes": 2}}
        m = self._manifest(files_map=files)
        self.assertEqual(list(m["files"]), ["a.md", "b.md"])
        self.assertTrue(json.dumps(m, ensure_ascii=False))


class DecideRelease(unittest.TestCase):
    def test_happy_path_local_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            index = write_backing(run_dir,
                                  [g for g, s in PASS_ALL_LOCAL.items() if s == "PASS"])
            report = make_report(PASS_ALL_LOCAL)
            report["_out_dir"] = str(run_dir)
            self.assertTrue(br.decide_release(report, index, "COMMIT1"))

    def test_commit_drift_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            index = write_backing(run_dir, ["STATIC_RULE_GATES", "CLI_ENTRYPOINT_E2E",
                                            "LOCAL_PIPELINE_E2E"])
            report = make_report(PASS_ALL_LOCAL)
            report["_out_dir"] = str(run_dir)
            self.assertFalse(br.decide_release(report, index, "OTHER_COMMIT"))

    def test_local_gate_fail_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            statuses = dict(PASS_ALL_LOCAL, LOCAL_PIPELINE_E2E="FAIL")
            index = write_backing(run_dir, ["STATIC_RULE_GATES", "CLI_ENTRYPOINT_E2E"])
            report = make_report(statuses)
            report["_out_dir"] = str(run_dir)
            self.assertFalse(br.decide_release(report, index, "COMMIT1"))

    def test_pass_gate_without_backing_file_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            index = write_backing(run_dir, ["STATIC_RULE_GATES", "CLI_ENTRYPOINT_E2E"])
            report = make_report(PASS_ALL_LOCAL)  # LOCAL 声称 PASS 但没有索引文件
            report["_out_dir"] = str(run_dir)
            self.assertFalse(br.decide_release(report, index, "COMMIT1"),
                             "PASS 门必须被具体索引文件背书")

    def test_faked_external_pass_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            statuses = dict(PASS_ALL_LOCAL, REAL_AGENT_E2E="PASS")
            index = write_backing(run_dir, ["STATIC_RULE_GATES", "CLI_ENTRYPOINT_E2E",
                                            "LOCAL_PIPELINE_E2E", "REAL_AGENT_E2E"])
            report = make_report(statuses)
            report["_out_dir"] = str(run_dir)
            self.assertFalse(br.decide_release(report, index, "COMMIT1"),
                             "外部门 PASS 缺 per-case 原件必须判否")

    def test_tampered_index_byte_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            index = write_backing(run_dir, ["STATIC_RULE_GATES", "CLI_ENTRYPOINT_E2E",
                                            "LOCAL_PIPELINE_E2E"])
            index[0]["sha256"] = "f" * 64  # 声称的哈希与磁盘字节不一致
            report = make_report(PASS_ALL_LOCAL)
            report["_out_dir"] = str(run_dir)
            self.assertFalse(br.decide_release(report, index, "COMMIT1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
