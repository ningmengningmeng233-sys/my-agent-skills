#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E6 异目录验收实跑器的自检：完整性门（SHA-256 必填）、SKIP/FAIL 分列、
三解释器全 PASS 才整体退 0、每次运行证据目录独立。
"""
import hashlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "测试"))

import run_linux_acceptance as rla  # noqa: E402  （脚本在 测试/ 根，需先补 sys.path）

MATRIX = "python3.10 python3.11 python3.12"


def _make_pkg_zip(root: Path, inner: str = "资产提取") -> Path:
    """合成一个只有最小骨架的候选包（用于验证解压/汇总逻辑，不跑真实门）。"""
    p = root / "pkg.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(f"{inner}/SKILL.md", "# t\n")
        z.writestr(f"{inner}/测试/e2e_flow.py", "# stub\n")
    return p


_STEP_MARK = {"snapshot": "--snapshot", "gate": "run_all_checks.py", "compileall": "compileall"}


def _fake_run_step(codes: dict[str, int] | None = None, default: int = 0):
    """替身：按步骤返回退出码，并真的落一个日志文件（保持日志路径逻辑被覆盖）。

    步骤靠 argv 里的标记识别（gate 步骤的 argv 只有 run_all_checks.py 路径，没有"gate"字样）。
    """
    codes = codes or {}

    def _run(pkg: Path, argv: list[str], log_path: Path) -> int:
        log_path.write_text("fake log\n", encoding="utf-8")
        joined = " ".join(str(a) for a in argv)
        for step, code in codes.items():
            if _STEP_MARK[step] in joined:
                return code
        return default

    return _run


def _fake_interpreter_ok(usable: set[str]):
    def _ok(python: str):
        if python in usable:
            return True, "3.x.y"
        return False, f"无法执行/缺依赖（{python}）"

    return _ok


class TestRunnerContract(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.zip_path = _make_pkg_zip(self.root)
        self.sha = hashlib.sha256(self.zip_path.read_bytes()).hexdigest()
        self.out = self.root / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def _argv(self, **kw):
        base = {"--zip": str(self.zip_path), "--out": str(self.out),
                "--expected-sha256": self.sha}
        base.update(kw)
        argv = ["run_linux_acceptance.py"]
        for k, v in base.items():
            if v is True:          # 布尔开关：只加开关名，不带值
                argv.append(k)
            else:
                argv += [k, v]
        if "--python-list" not in base and "--python" not in base:
            argv += ["--python", "python_stub"]
        return argv

    def _run_main(self, **kw):
        with mock.patch.object(sys, "argv", self._argv(**kw)):
            return rla.main()

    def _rows(self, run_dir: Path) -> list[list[str]]:
        lines = (run_dir / "results.tsv").read_text(encoding="utf-8").strip().splitlines()
        return [l.split("\t") for l in lines[1:]]

    # ---- 完整性门：SHA-256 必填 ----

    def test_expected_sha256_is_required(self):
        # 不要求期望值就能"通过"，等于没做完整性验收
        argv = ["run_linux_acceptance.py", "--zip", str(self.zip_path), "--out", str(self.out),
                "--python", "python_stub"]
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(rla.main(), 2)

    def test_hash_mismatch_refuses_before_extracting(self):
        with mock.patch.object(rla, "_interpreter_ok", _fake_interpreter_ok({"python_stub"})):
            self.assertEqual(self._run_main(**{"--expected-sha256": "deadbeef"}), 2)
        run_dirs = list(self.out.glob("run-*")) if self.out.is_dir() else []
        self.assertEqual(run_dirs, [], "完整性失败时不得解压/不得产生运行目录")

    def test_missing_zip_returns_2(self):
        self.assertEqual(self._run_main(**{"--zip": str(self.root / "没有.zip")}), 2)

    # ---- 汇总语义：三解释器全 PASS 才退 0 ----

    def test_matrix_all_pass_exits_zero(self):
        with mock.patch.object(rla, "_interpreter_ok", _fake_interpreter_ok(set(MATRIX.split()))), \
             mock.patch.object(rla, "_run_step", _fake_run_step()):
            rc = self._run_main(**{"--python-list": MATRIX})
        self.assertEqual(rc, 0)
        run_dir = [p for p in self.out.glob("run-*")][0]
        rows = self._rows(run_dir)
        self.assertEqual(len(rows), 9, "3 解释器 × 3 步骤")
        self.assertTrue(all(r[2] == "PASS" for r in rows), rows)
        self.assertTrue(all(r[3] == "0" for r in rows), rows)

    def test_matrix_one_pass_two_skips_is_nonzero(self):
        # 只有 3.12 真跑了：整体绝不能算通过
        with mock.patch.object(rla, "_interpreter_ok", _fake_interpreter_ok({"python3.12"})), \
             mock.patch.object(rla, "_run_step", _fake_run_step()):
            rc = self._run_main(**{"--python-list": MATRIX})
        self.assertNotEqual(rc, 0)
        self.assertEqual(rc, 3, "无 FAIL 但存在 SKIP：应给出可区分的非零码")
        rows = self._rows([p for p in self.out.glob("run-*")][0])
        statuses = {r[0]: set() for r in rows}
        for r in rows:
            statuses[r[0]].add(r[2])
        self.assertEqual(statuses["python3.12"], {"PASS"})
        self.assertEqual(statuses["python3.10"], {"SKIP"})
        self.assertEqual(statuses["python3.11"], {"SKIP"})
        self.assertNotIn("FAIL", {r[2] for r in rows})

    def test_matrix_failure_is_nonzero_and_marked_fail(self):
        with mock.patch.object(rla, "_interpreter_ok", _fake_interpreter_ok(set(MATRIX.split()))), \
             mock.patch.object(rla, "_run_step", _fake_run_step({"gate": 1})):
            rc = self._run_main(**{"--python-list": MATRIX})
        self.assertEqual(rc, 1)
        rows = self._rows([p for p in self.out.glob("run-*")][0])
        fails = [r for r in rows if r[2] == "FAIL"]
        self.assertEqual(len(fails), 3, "每个解释器的 gate 都应记 FAIL")
        self.assertEqual({r[1] for r in fails}, {"gate"})
        self.assertEqual({r[3] for r in fails}, {"1"})

    def test_single_interpreter_skip_is_nonzero(self):
        with mock.patch.object(rla, "_interpreter_ok", _fake_interpreter_ok(set())):
            rc = self._run_main()
        self.assertEqual(rc, 3)

    # ---- 证据目录隔离 ----

    def test_default_run_dirs_are_unique_and_isolated(self):
        with mock.patch.object(rla, "_interpreter_ok", _fake_interpreter_ok({"python_stub"})), \
             mock.patch.object(rla, "_run_step", _fake_run_step()):
            self.assertEqual(self._run_main(), 0)
            first = [p for p in self.out.glob("run-*")]
            self.assertEqual(len(first), 1)
            self.assertEqual(self._run_main(), 0)
        runs = sorted(self.out.glob("run-*"))
        self.assertEqual(len(runs), 2, "同一次 --out 下的两次运行必须各自独立")
        self.assertNotEqual(runs[0].name, runs[1].name)
        for rd in runs:
            self.assertEqual(len(self._rows(rd)), 3)

    def test_run_dir_reuse_refused_then_allowed_without_stale_rows(self):
        run_dir = self.root / "固定证据目录"
        with mock.patch.object(rla, "_interpreter_ok", _fake_interpreter_ok({"python_stub"})), \
             mock.patch.object(rla, "_run_step", _fake_run_step({"gate": 1})):
            self.assertEqual(self._run_main(**{"--run-dir": str(run_dir)}), 1)
        before = self._rows(run_dir)
        with mock.patch.object(rla, "_interpreter_ok", _fake_interpreter_ok({"python_stub"})), \
             mock.patch.object(rla, "_run_step", _fake_run_step()):
            # 不给 --reuse-run-dir：拒绝，避免把上一轮的行混进本轮
            self.assertEqual(self._run_main(**{"--run-dir": str(run_dir)}), 2)
            self.assertEqual(self._rows(run_dir), before, "拒绝时不得改动既有证据")
            # 显式复用：旧表归档留痕、新表只装本轮
            self.assertEqual(self._run_main(**{"--run-dir": str(run_dir), "--reuse-run-dir": True}), 0)
        rows = self._rows(run_dir)
        self.assertEqual(len(rows), 3, f"本轮结果表不得含旧行: {rows}")
        self.assertTrue(all(r[2] == "PASS" for r in rows))
        self.assertTrue(list(run_dir.glob("results.prev-*.tsv")), "旧证据应归档而非删除")

    # ---- 既有保护 ----

    def test_sha256_and_extract_fresh_normalizes_root(self):
        self.assertEqual(rla._sha256(self.zip_path), self.sha)
        dest = self.root / "unpack-x"
        (dest / "旧残留").mkdir(parents=True)
        pkg = rla._extract_fresh(self.zip_path, dest)
        self.assertEqual(pkg.name, "资产提取")
        self.assertTrue((pkg / "SKILL.md").is_file())
        self.assertFalse((dest / "旧残留").exists())

    def test_child_env_strips_encoding_umbrella(self):
        with mock.patch.dict("os.environ", {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}, clear=False):
            env = rla._child_env()
        self.assertNotIn("PYTHONUTF8", env)
        self.assertNotIn("PYTHONIOENCODING", env)


if __name__ == "__main__":
    unittest.main()
