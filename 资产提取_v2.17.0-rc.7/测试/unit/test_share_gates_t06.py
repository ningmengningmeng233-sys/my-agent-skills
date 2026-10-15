# -*- coding: utf-8 -*-
"""T06 分享包闸门：许可未确认与用户数据不得进 shareable。

现场实测口径（本文件把它钉住）：
- 未登记进风格包 manifest 的图片 → 私有档中止打包（fail-closed），分享档排除并声明
  VISUAL_STYLE_INCOMPLETE_FOR_SHARE；两条路都不许静默放行。
- 登记为 share_allowed=false 的媒体 → 私有档保留（要能完整安装），分享档排除并给原因。
- 运行期日志两档都不发（EXCLUDE_SUFFIXES 既有行为）；本地库文件（.sqlite/.db）与
  用户数据目录只在分享档排除，且必须留下可审计的排除原因。
- 策略文件缺新键时按构建器兜底最严口径处理，不给「没写就放行」留口子。
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
_spec = importlib.util.spec_from_file_location("br_t06", _ROOT / "scripts" / "build_release.py")
br = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(br)

_h_spec = importlib.util.spec_from_file_location("br_harness_t06",
                                                 _HERE.parent / "test_build_release.py")
_h = importlib.util.module_from_spec(_h_spec)
_h_spec.loader.exec_module(_h)

PACK = "references/styles/风格X/1.0.0"
GHOST = f"{PACK}/private/references/ghost.png"
KEPT_OUT = f"{PACK}/private/references/keep-out.png"


def _names(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return {n.replace("\\", "/") for n in zf.namelist()}


def _staged(source: Path, profile: str, policy: dict | None = None):
    files, excluded = br.plan_staging(source, profile, policy)
    return {p.relative_to(source).as_posix() for p in files}, excluded


class ShareGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src = _h._make_fake_source(self.root)
        self.out = self.root / "dist"

    def tearDown(self):
        self._tmp.cleanup()

    def _add(self, rel: str, data: bytes = b"x") -> str:
        p = self.src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return rel

    def test_unregistered_image_aborts_private_and_is_dropped_from_shareable(self):
        self._add(GHOST, b"\x89PNG\r\n\x1a\nghost")
        with self.assertRaises(SystemExit) as ctx:
            br.build_release(self.src, self.out, "private")
        self.assertIn("未登记", str(ctx.exception))
        shareable = br.build_release(self.src, self.out, "shareable")
        self.assertFalse(any("ghost.png" in n for n in _names(shareable)))
        _files, excluded = _staged(self.src, "shareable")
        hit = [e for e in excluded if e["path"] == GHOST]
        self.assertTrue(hit and "许可" in hit[0]["reason"], excluded)

    def test_registered_but_unshared_image_keeps_private_and_leaves_shareable(self):
        manifest = self.src / PACK / "manifest.toml"
        manifest.write_text(manifest.read_text(encoding="utf-8") + "\n".join([
            "", "[[files]]", 'path = "references/keep-out.png"',
            'sha256 = "' + "e" * 64 + '"', "bytes = 12", 'category = "视图"',
            "share_allowed = false", "required = false", "",
        ]), encoding="utf-8", newline="\n")
        self._add(KEPT_OUT, b"\x89PNG\r\nkeepout")
        private, _ = _staged(self.src, "private")
        self.assertIn(KEPT_OUT, private, "私有包必须能完整安装")
        shared, excluded = _staged(self.src, "shareable")
        self.assertNotIn(KEPT_OUT, shared)
        self.assertTrue(any(e["path"] == KEPT_OUT and "不可分享" in e["reason"]
                            for e in excluded), excluded)

    def test_runtime_logs_are_out_of_both_tiers(self):
        rel = self._add("docs/运行记录.log", b"trace\n")
        for profile in ("private", "shareable"):
            staged, _ = _staged(self.src, profile)
            self.assertNotIn(rel, staged, f"{profile} 不该带运行期日志")

    def test_local_database_and_user_data_leave_only_the_shareable_tier(self):
        db = self._add("references/state.sqlite", b"SQLite format 3\x00")
        lib = self._add("references/用户库/某本书.txt", b"manuscript\n")
        private, _ = _staged(self.src, "private")
        self.assertIn(db, private)
        self.assertIn(lib, private)
        shared, excluded = _staged(self.src, "shareable")
        self.assertNotIn(db, shared)
        self.assertNotIn(lib, shared)
        reasons = {e["path"]: e["reason"] for e in excluded}
        self.assertIn("运行期", reasons.get(db, ""), reasons.get(db))
        self.assertIn("用户数据", reasons.get(lib, ""), reasons.get(lib))

    def test_missing_policy_keys_fall_back_to_the_strict_minimum(self):
        minimal = {"shareable_exclude_prefixes": []}
        shared, _ = _staged(self.src, "shareable", minimal)
        self.assertFalse(any(n.endswith((".log", ".sqlite")) for n in shared),
                         "老策略没有新键时也必须按兜底排除")
        real = br.load_share_policy(_ROOT)
        for key in ("shareable_exclude_prefixes", "shareable_exclude_suffixes",
                    "shareable_exclude_name_substrings", "shareable_media_suffixes"):
            self.assertIn(key, real, f"仓库策略文件必须显式声明 {key}")

    def test_shareable_declares_incompleteness(self):
        import json
        zip_path = br.build_release(self.src, self.out, "shareable")
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if n.endswith(".json")
                     and ("manifest" in n or "清单" in n)]
            self.assertTrue(names, "包内必须带机器可读清单，第三方才能复核裁剪与能力声明")
            data = json.loads(zf.read(names[0]).decode("utf-8"))
        self.assertIn(br.INCOMPLETE_DECLARATION, data.get("declarations", []),
                      "来源图被裁剪后必须声明与私有包不等价")
        self.assertTrue(data.get("excluded"), "排除项必须逐条留原因")


class ShareExemptScannerToolTest(unittest.TestCase):
    """敏感词按路径子串匹配，曾把「找密钥的工具本身」一起裁掉：
    实测分享包内 测试/run_all_checks.py 跑出 14/17，缺件正是 scripts/secret_scan.py。
    收件人自门不能因为「文件名里有 secret」而失去密钥闸。"""

    def _policy(self):
        return {"shareable_exclude_name_substrings": ["token", "cookie", "secret", "用户库"],
                "shareable_exclude_exempt": ["scripts/secret_scan.py"]}

    def test_declared_scanner_tool_is_kept(self):
        self.assertEqual(br._shareable_excluded("scripts/secret_scan.py", self._policy(), {}), "")

    def test_real_sensitive_files_still_excluded(self):
        policy = self._policy()
        for rel in ("config/secrets.json", "token_dump.txt", "数据/用户库/a.md"):
            self.assertTrue(br._shareable_excluded(rel, policy, {}), rel)

    def test_exemption_is_exact_path_not_prefix(self):
        self.assertTrue(br._shareable_excluded("scripts/secret_scan_backup.py", self._policy(), {}))

    def test_repo_policy_must_declare_the_exemption(self):
        real = br.load_share_policy(_ROOT)
        self.assertIn("scripts/secret_scan.py", real.get("shareable_exclude_exempt", []),
                      "仓库策略要显式豁免自带密钥扫描器，否则包内自门缺件")


class FixtureMediaAndVendorTest(unittest.TestCase):
    """分享包裁剪曾把技能自产测试夹具图与跨技能总账写入器一起裁掉，
    结果包内自测在收件人机器上必红（实测 3 failed）。两条都要有正反例。"""

    def _policy(self):
        return {"shareable_media_suffixes": [".png"],
                "shareable_media_exempt_prefixes": ["测试/fixtures/legacy_visual/"]}

    def test_own_fixture_image_is_kept(self):
        self.assertEqual(
            br._shareable_excluded("测试/fixtures/legacy_visual/old.png", self._policy(), {}),
            "")

    def test_stylepack_image_still_needs_registration(self):
        rel = "references/styles/风格X/1.0.0/references/face.png"
        reason = br._shareable_excluded(rel, self._policy(), {})
        self.assertTrue(reason, "未登记的风格包参考图仍必须裁掉")
        self.assertIn("登记", reason)

    def test_repo_policy_declares_fixture_exempt_and_vendor(self):
        real = br.load_share_policy(_ROOT)
        self.assertIn("测试/fixtures/legacy_visual/",
                      real.get("shareable_media_exempt_prefixes", []))
        self.assertIn("vendor", br.WHITELIST, "包内受控副本目录必须入白名单")

    def test_vendored_ledger_exists_and_resolves(self):
        vendor = _ROOT / "vendor" / "schemas" / "ledger.py"
        self.assertTrue(vendor.is_file(), "收件人单独安装时靠包内副本记总账")
        self.assertTrue((_ROOT / "vendor" / "schemas" / "VENDORED.md").is_file(),
                        "受控副本必须写清来源与理由")


if __name__ == "__main__":
    unittest.main(verbosity=2)
