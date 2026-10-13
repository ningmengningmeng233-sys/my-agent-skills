"""D1 guard：凭证协议校验器（`scripts/verification_protocol.py`）。

覆盖六层（工单 S5 Task D1 三条红线）：
1. 仓内真实协议文件必须校验干净（validate == []，CLI rc=0）；
2. PASS 无凭据即判红：缺日期 / 缺产物路径 / 产物文件不存在 / 缺 note 全部拒绝；
3. BLOCKED-CRED 必须写 reason + unlock_condition + recheck_command，缺一判红；
4. 渠道只许登记取用方式：key_value_policy 非 REPLACE 判红；
5. 协议文件出现任何形似真实密钥的字面量（sk- 前缀、32-40 位 hex、20+ 位字母数字混合、
   key_value 字段）一律判红——假 key 只在内存里构造，绝不写入仓内文件；
6. 登记表完整性：records 为空 / 未知档位 / 重复档位 / status 非法均判红；
   CLI 对缺失协议文件返回 3、对不合法内容返回 2。

运行（目录名不是合法 Python 标识符，禁止 `-m unittest` 模块路径）：

    python "_审查测试/test_verification_protocol.py" -v
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import verification_protocol as vp  # noqa: E402

from release_surface import ReleaseSurface  # noqa: E402

PROTOCOL_FILE = SKILL_ROOT / "references" / "protocol" / "verification-protocol.json"


def real_doc() -> dict:
    return json.loads(PROTOCOL_FILE.read_text(encoding="utf-8"))


def record(doc: dict, gate: str) -> dict:
    return next(r for r in doc["records"] if r.get("gate") == gate)


# 假密钥只在内存构造，避免仓内任何文件出现可疑字面量。
FAKE_SK_KEY = "sk-" + "A1b2" * 6
FAKE_HEX_KEY = "0a1b" * 8  # 32 位十六进制
FAKE_MIXED_KEY = "a1b2c3d4e5f6g7h8i9j0"  # 20 位字母数字混合


class RealProtocolTest(unittest.TestCase):
    def test_repo_protocol_file_validates_clean(self):
        self.assertEqual(vp.validate(real_doc()), [])

    def test_repo_protocol_file_exists(self):
        self.assertTrue(PROTOCOL_FILE.is_file(), "凭证协议模板必须入库")

    def test_protocol_registers_no_secret_values(self):
        doc = real_doc()
        for ch in doc["channels"]:
            self.assertNotIn("key_value", ch, "渠道只登记取用方式，不得存密钥值")
            self.assertEqual(ch["key_value_policy"], "REPLACE")

    def test_cli_check_exits_zero(self):
        from conftest import run_py

        proc = run_py([str(SKILL_ROOT / "scripts" / "verification_protocol.py"), "check"],
                      cwd=str(SKILL_ROOT))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class PassEvidenceGateTest(unittest.TestCase):
    """红线一：PASS 必须带日期 + 真实存在的产物路径 + note，否则判红。"""

    def _mutate(self, fn):
        doc = real_doc()
        fn(record(doc, "STATIC_RULE_GATES"))
        return vp.validate(doc)

    def test_pass_without_date_rejected(self):
        reasons = self._mutate(lambda r: r.update(date=""))
        self.assertTrue(any("date" in x for x in reasons), reasons)

    def test_pass_without_artifact_paths_rejected(self):
        reasons = self._mutate(lambda r: r.update(artifact_paths=[]))
        self.assertTrue(any("产物路径" in x for x in reasons), reasons)

    def test_pass_missing_artifact_key_rejected(self):
        reasons = self._mutate(lambda r: r.pop("artifact_paths"))
        self.assertTrue(any("产物路径" in x for x in reasons), reasons)

    def test_pass_artifact_path_not_on_disk_rejected(self):
        reasons = self._mutate(
            lambda r: r.update(artifact_paths=["_审查测试/不存在的产物文件.md"]))
        self.assertTrue(any("不存在" in x for x in reasons), reasons)

    def test_pass_without_note_rejected(self):
        reasons = self._mutate(lambda r: r.update(note=""))
        self.assertTrue(any("note" in x for x in reasons), reasons)

    def test_pass_date_wrong_format_rejected(self):
        reasons = self._mutate(lambda r: r.update(date="2026-9-3"))
        self.assertTrue(any("YYYY-MM-DD" in x for x in reasons), reasons)


class BlockedCredTest(unittest.TestCase):
    """红线二：BLOCKED-CRED 必须写清原因 + 解锁条件 + 复核命令。"""

    def _mutate(self, fn):
        doc = real_doc()
        fn(record(doc, "REAL_AGENT_E2E"))
        return vp.validate(doc)

    def test_blocked_without_unlock_condition_rejected(self):
        reasons = self._mutate(lambda r: r.update(unlock_condition="  "))
        self.assertTrue(any("unlock_condition" in x for x in reasons), reasons)

    def test_blocked_without_recheck_command_rejected(self):
        reasons = self._mutate(lambda r: r.pop("recheck_command"))
        self.assertTrue(any("recheck_command" in x for x in reasons), reasons)

    def test_blocked_without_reason_rejected(self):
        reasons = self._mutate(lambda r: r.pop("reason"))
        self.assertTrue(any("reason" in x for x in reasons), reasons)


class ChannelPolicyTest(unittest.TestCase):
    def test_key_value_policy_not_replace_rejected(self):
        doc = real_doc()
        doc["channels"][0]["key_value_policy"] = "KEEP"
        reasons = vp.validate(doc)
        self.assertTrue(any("REPLACE" in x for x in reasons), reasons)

    def test_channel_missing_required_field_rejected(self):
        doc = real_doc()
        doc["channels"][0].pop("key_env")
        reasons = vp.validate(doc)
        self.assertTrue(any("key_env" in x for x in reasons), reasons)


class SecretLiteralTest(unittest.TestCase):
    """红线三：形似真实密钥的字面量一律判红（只在内存构造，不写入仓内文件）。"""

    def _doc_with(self, value):
        doc = copy.deepcopy(real_doc())
        doc["records"][0]["note"] = value
        return doc

    def test_sk_prefix_literal_rejected(self):
        reasons = vp.validate(self._doc_with(FAKE_SK_KEY))
        self.assertTrue(any("密钥" in x for x in reasons), reasons)

    def test_hex_literal_rejected(self):
        reasons = vp.validate(self._doc_with(FAKE_HEX_KEY))
        self.assertTrue(any("密钥" in x for x in reasons), reasons)

    def test_long_alnum_mixed_literal_rejected(self):
        reasons = vp.validate(self._doc_with(FAKE_MIXED_KEY))
        self.assertTrue(any("密钥" in x for x in reasons), reasons)

    def test_key_value_field_rejected_whatever_its_content(self):
        doc = copy.deepcopy(real_doc())
        doc["channels"][0]["key_value"] = "见聊天记录"
        reasons = vp.validate(doc)
        self.assertTrue(any("key_value" in x for x in reasons), reasons)

    def test_key_value_policy_field_must_not_trip_secret_scan(self):
        # 回归护栏：合法字段名 key_value_policy 不得被密钥扫描误杀。
        doc = copy.deepcopy(real_doc())
        doc["channels"][0]["key_value_policy"] = "REPLACE"
        self.assertFalse(any("不得存密钥值" in x for x in vp.validate(doc)))


class RegistryIntegrityTest(unittest.TestCase):
    def test_empty_records_rejected(self):
        doc = real_doc()
        doc["records"] = []
        self.assertTrue(any("records" in x for x in vp.validate(doc)))

    def test_unknown_gate_rejected(self):
        doc = real_doc()
        doc["records"][0]["gate"] = "MADE_UP_GATE"
        reasons = vp.validate(doc)
        self.assertTrue(any("未知档位" in x for x in reasons), reasons)

    def test_duplicate_gate_rejected(self):
        doc = real_doc()
        doc["records"].append(copy.deepcopy(doc["records"][0]))
        reasons = vp.validate(doc)
        self.assertTrue(any("重复" in x for x in reasons), reasons)

    def test_illegal_status_rejected(self):
        doc = real_doc()
        record(doc, "LOCAL_PIPELINE_E2E")["status"] = "PASSED-LIKE"
        reasons = vp.validate(doc)
        self.assertTrue(any("status 非法" in x for x in reasons), reasons)

    def test_gate_order_is_single_sourced_from_closure_contract(self):
        import closure_gate_contract
        self.assertEqual(vp.GATE_ORDER, closure_gate_contract.GATE_ORDER)


class CliExitCodeTest(unittest.TestCase):
    def _run_main_with(self, protocol_path, doc=None):
        original = vp.PROTOCOL_PATH
        vp.PROTOCOL_PATH = protocol_path
        try:
            return vp.main([])
        finally:
            vp.PROTOCOL_PATH = original

    def test_missing_protocol_file_returns_3(self):
        with tempfile.TemporaryDirectory() as td:
            rc = self._run_main_with(Path(td) / "nope.json")
            self.assertEqual(rc, 3)

    def test_broken_protocol_file_returns_2(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.json"
            doc = real_doc()
            record(doc, "STATIC_RULE_GATES")["date"] = ""
            bad.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            rc = self._run_main_with(bad)
            self.assertEqual(rc, 2)


class RegistryCompletenessTest(unittest.TestCase):
    """红线四（收尾轮新增）：六档逐档登记，且 PASS 必须指向一次真实运行的日志。

    上一版只要求「records 非空」，于是协议可以漏登记最难看的档位；PASS 的「产物」
    也可以只是两份源码文件——源码存在不等于跑过，正是 §1.3 禁止的「静态审计冒充
    真实生成」。本轮又实测出采集器登记表腐坏导致静态段/CLI 段长期假红，所以这两档
    必须带着自己的运行日志出现在协议里，红绿都能被复核。
    """

    def test_repo_protocol_registers_every_gate(self):
        gates = {r["gate"] for r in real_doc()["records"]}
        self.assertEqual(set(vp.GATE_ORDER), gates)

    def test_missing_gate_record_rejected(self):
        doc = real_doc()
        doc["records"] = [r for r in doc["records"]
                          if r["gate"] != "CLI_ENTRYPOINT_E2E"]
        reasons = vp.validate(doc)
        self.assertTrue(any("未登记" in x for x in reasons), reasons)

    def test_pass_without_run_log_rejected(self):
        doc = real_doc()
        record(doc, "STATIC_RULE_GATES")["artifact_paths"] = ["scripts/audit_cli.py"]
        reasons = vp.validate(doc)
        self.assertTrue(any("运行证据" in x for x in reasons), reasons)

    def test_repo_pass_records_cite_existing_run_logs(self):
        doc = real_doc()
        surface = ReleaseSurface(SKILL_ROOT)
        checked = 0
        withheld = 0
        for rec in doc["records"]:
            if rec["status"] != "PASS":
                continue
            logs = [p for p in rec["artifact_paths"] if p.startswith("evidence/")]
            self.assertTrue(logs, f"{rec['gate']} 声称 PASS 却没有 evidence/ 运行日志")
            for p in logs:
                ok, why = surface.check(p)
                self.assertTrue(ok, f"{p}: {why}")
                checked += 1
                if "裁剪" in why:
                    withheld += 1
        self.assertGreater(checked, 0, "PASS 记录一条产物都没查，等于没验")
        if not (SKILL_ROOT / ".git").exists():
            self.assertGreater(withheld, 0, "发布包里日志按档位裁剪，必须显式判成 withheld_covered")


if __name__ == "__main__":
    unittest.main(verbosity=2)
