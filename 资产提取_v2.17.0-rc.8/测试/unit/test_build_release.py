#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F5：build_release 白名单打包契约——敏感文件绝不入包、白名单齐全、清单哈希一致。"""
import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scripts.build_release import WHITELIST, _staged_files, _sensitive_scan, build_release


RUNTIME_PNG = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10]) + b"runtime"
RUNTIME_SHA = hashlib.sha256(RUNTIME_PNG).hexdigest()


def _make_fake_source(root: Path) -> Path:
    src = root / "技能仓库"
    files = {
        # 白名单最小集
        "SKILL.md": '---\nname: asset-extract-test\nversion: "2.14.0-rc.1"\n---\n# t\n',
        "README.md": "# t\n版本 2.14.0-rc.1\n",
        "QUICKSTART.md": "# 快速开始\n",
        "FAQ.md": "# FAQ\n",
        "CONTEXT.md": "# 上下文\n",
        "INSTALL.md": "# 安装\n",
        "pyproject.toml": '[project]\nversion = "2.14.0rc1"\n',
        "requirements.txt": "requests>=2.25\n",
        ".env.example": "OPENAI_API_KEY=\n",
        ".flake8": "[flake8]\nmax-line-length = 100\nextend-ignore = E203, E402, E501, E741\n",
        # 已批准的真实根级交付合同（F34/F-22）：可执行入口、节点池样例与许可正文都是具名必需件，
        # 合成源必须按同一合同给全，否则测的是「fixture 不全」而不是打包规则。
        "run_pipeline.sh": "#!/usr/bin/env bash\necho 'synthetic entry'\n",
        "run_pipeline.bat": "@echo off\r\nrem synthetic entry\r\n",
        "llm_nodes.json.example": json.dumps({"nodes": []}, ensure_ascii=False) + "\n",
        "LICENSE": "Proprietary - synthetic license text for tests\n",
        "scripts/keep.py": "print('ok')\n",
        "references/卡.md": "卡\n",
        "templates/模板.md": "模板\n",
        "服饰设计/校验.md": "校验\n",
        "docs/说明.md": "说明\n",
        "测试/unit/test_x.py": "def test_x():\n    assert True\n",
        "测试/run_all_checks.py": "print('gate')\n",
        # 必须被排除的敏感/垃圾物
        ".env": "OPENAI_API_KEY=sk-fake-key-0123456789abcdef\n",
        "资产库/用户资产.md": "用户真实资产，绝不入包\n",
        "测试/e2e/资产库/反推_最终.md": "快照\n",
        "scripts/__pycache__/keep.pyc": b"\x00binary",
        ".asset_state.db": b"sqlite",
        "测试/e2e/临时快照.md": "snap\n",
        # 迁移夹具：目录名恰为「资产库」，但位于 测试/fixtures/ 下，必须入包（F5 烟测依赖）
        "测试/fixtures/v212_minimal/资产库/反推_最终.md": "夹具\n",
    }
    # 分享策略与风格包（双包打包必需；来源图必须只进私人包）
    extra_text = {
        "references/styles/share-policy.toml": "\n".join([
            "schema_version = 1",
            'shareable_exclude_prefixes = ['
            '"references/styles/风格X/1.0.0/private/references/sources/"]',
            "shareable_exclude_names = []",
            'required_for_runtime = ["generation_formula.md", "negative_prompt.md", '
            '"references/router.md", "references/shared_style_invariants.md", '
            '"references/face_base_style.md", "references/full_body_base_style.md", '
            '"references/environment_base_style.md", "references/object_base_style.md"]',
            'private_package_name = "资产提取-v{version}-private.zip"',
            'shareable_package_name = "资产提取-v{version}-shareable.zip"',
            "",
        ]),
        "references/styles/风格X/1.0.0/private/generation_formula.md": "公式\n",
        "references/styles/风格X/1.0.0/private/negative_prompt.md": "负面\n",
        "references/styles/风格X/1.0.0/private/references/router.md": "路由\n",
        "references/styles/风格X/1.0.0/private/references/shared_style_invariants.md": "不变量\n",
        "references/styles/风格X/1.0.0/private/references/face_base_style.md": "脸\n",
        "references/styles/风格X/1.0.0/private/references/full_body_base_style.md": "全身\n",
        "references/styles/风格X/1.0.0/private/references/environment_base_style.md": "环境\n",
        "references/styles/风格X/1.0.0/private/references/object_base_style.md": "物件\n",
        "references/styles/风格X/1.0.0/manifest.toml": "\n".join([
            "schema_version = 1", 'style = "风格X"', 'version = "1.0.0"', "",
            "[[files]]", 'path = "references/sources/character-1.png"',
            'sha256 = "' + "a" * 64 + '"', "bytes = 15", 'category = "来源图"',
            "share_allowed = false", "required = false", "",
            "[[files]]", 'path = "references/face.png"',
            'sha256 = "' + RUNTIME_SHA + '"', f"bytes = {len(RUNTIME_PNG)}", 'category = "运行图"',
            "share_allowed = true", "required = true", "",
            "[[files]]", 'path = "references/nolicense.png"',
            'sha256 = "' + RUNTIME_SHA + '"', f"bytes = {len(RUNTIME_PNG)}", 'category = "运行图"',
            "share_allowed = false", "required = true", "",
        ]),
    }
    for rel, content in extra_text.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")
    src_image = src / "references/styles/风格X/1.0.0/private/references/sources/character-1.png"
    src_image.parent.mkdir(parents=True, exist_ok=True)
    src_image.write_bytes(b"\x89PNG\r\n\x1a\nsrc")
    # 已登记运行必需图：与真实包的 20 张同形状（share_allowed=true、required=true、哈希相符）
    runtime_image = src / "references/styles/风格X/1.0.0/private/references/face.png"
    runtime_image.parent.mkdir(parents=True, exist_ok=True)
    runtime_image.write_bytes(RUNTIME_PNG)
    # 同一形状再钉一张「登记但 share_allowed=false」的图：走的是真实排除通道
    nolic = src / "references/styles/风格X/1.0.0/private/references/nolicense.png"
    nolic.write_bytes(RUNTIME_PNG)
    for rel, content in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8", newline="\n")
    return src


class TestBuildRelease(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src = _make_fake_source(self.root)
        self.out = self.root / "发布目录"  # 仓库外输出

    def tearDown(self):
        self._tmp.cleanup()

    def test_staging_excludes_sensitive(self):
        files = _staged_files(self.src)
        rels = {p.relative_to(self.src).as_posix() for p in files}
        self.assertIn("SKILL.md", rels)
        self.assertIn("scripts/keep.py", rels)
        self.assertIn("测试/unit/test_x.py", rels)
        self.assertNotIn(".env", rels)
        self.assertNotIn("资产库/用户资产.md", rels)
        self.assertNotIn("测试/e2e/资产库/反推_最终.md", rels)
        self.assertNotIn("scripts/__pycache__/keep.pyc", rels)
        self.assertNotIn(".asset_state.db", rels)
        # fixtures 夹具（目录名为资产库）必须保留；开发库与 e2e 快照必须排除
        self.assertIn("测试/fixtures/v212_minimal/资产库/反推_最终.md", rels)
        self.assertNotIn("资产库/用户资产.md", rels)
        # 排除目录过滤：e2e 下任何文件都不进暂存
        self.assertFalse([r for r in rels if r.startswith("测试/e2e/")])

    def test_sensitive_scan_aborts(self):
        files = _staged_files(self.src) + [self.src / ".env"]
        with self.assertRaises(SystemExit):
            _sensitive_scan(files, self.src)

    def test_build_produces_zip_with_manifest(self):
        zip_path = build_release(self.src, self.out)
        self.assertTrue(zip_path.is_file())
        self.assertIn("2.14.0-rc.1", zip_path.name)
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            self.assertTrue(any(n.endswith("SKILL.md") for n in names))
            self.assertFalse(any(".env" == Path(n).name for n in names))
            self.assertFalse(any("用户资产" in n for n in names))
            self.assertFalse(any("/e2e/" in n.replace("\\", "/") for n in names))
            manifest = [n for n in names if n.endswith("SHA256SUMS.txt")]
            self.assertEqual(len(manifest), 1)
            lines = zf.read(manifest[0]).decode("utf-8").strip().splitlines()
            by_rel = {l.split("  ", 1)[1]: l.split("  ", 1)[0] for l in lines}
            self.assertIn("SKILL.md", by_rel)
            expected = hashlib.sha256((self.src / "SKILL.md").read_bytes()).hexdigest()
            self.assertEqual(by_rel["SKILL.md"], expected)

    def test_out_inside_source_rejected(self):
        with self.assertRaises(SystemExit):
            build_release(self.src, self.src / "发布")

    def test_whitelist_covers_runtime(self):
        for required in ("SKILL.md", "scripts", "templates", "references", "服饰设计", "测试", "pyproject.toml"):
            self.assertIn(required, WHITELIST)

    def test_flake8_config_is_packaged(self):
        # 解压副本要能自跑 run_all_checks；缺 .flake8 时 flake8 落回默认 79 列且不再忽略
        # E402/E501，候选包自己的质量门会假红（E5 解压验收实测踩到，回归钉死）
        self.assertIn(".flake8", WHITELIST)
        files = _staged_files(self.src)
        rels = {p.relative_to(self.src).as_posix() for p in files}
        self.assertIn(".flake8", rels)

    def test_real_repo_has_flake8_config(self):
        # 仓库真实 .flake8 必须存在，否则发布包永远补不上这份配置
        self.assertTrue((REPO_ROOT / ".flake8").is_file(), "仓库根缺 .flake8")


class TestDualProfileRelease(unittest.TestCase):
    """工单04-Task4：私人完整包与受控分享包分别构建，内容/名称/能力声明不同。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src = _make_fake_source(self.root)
        self.out = self.root / "发布目录"

    def tearDown(self):
        self._tmp.cleanup()

    def _names(self, zip_path):
        with zipfile.ZipFile(zip_path) as zf:
            return {n.replace("\\", "/") for n in zf.namelist()}

    def test_two_profiles_have_different_names_and_content(self):
        private = build_release(self.src, self.out, "private")
        shareable = build_release(self.src, self.out, "shareable")
        self.assertNotEqual(private.name, shareable.name)
        self.assertIn("private", private.name)
        self.assertIn("shareable", shareable.name)
        p_names = self._names(private)
        s_names = self._names(shareable)
        self.assertTrue(any("sources/character-1.png" in n for n in p_names))
        self.assertFalse(any("sources/character-1.png" in n for n in s_names),
                         "分享包必须排除来源图")

    def test_missing_named_root_deliverable_still_aborts(self):
        """合成源补全不等于放宽守卫：缺任一枚具名根级交付件仍必须拒绝出包。"""
        for missing in ("run_pipeline.bat", "LICENSE"):
            src = _make_fake_source(self.root / ("缺" + missing))
            (src / missing).unlink()
            with self.assertRaises(SystemExit) as ctx:
                build_release(src, self.root / ("出" + missing), "private")
            self.assertIn(missing, str(ctx.exception), str(ctx.exception))

    def test_private_carries_registered_runtime_image_and_shareable_does_not(self):
        """获准登记的运行图只进私有档；分享档必须不含这些私有媒体（原政策不变）。"""
        private = build_release(self.src, self.out, "private")
        shareable = build_release(self.src, self.out, "shareable")
        p_names = self._names(private)
        s_names = self._names(shareable)
        self.assertTrue(any("references/face.png" in n for n in p_names), p_names)
        # 真实机制：进不进分享档由**登记的许可位**决定（share_allowed=false 一律排除），
        # 而不是我另发明一条「图片都不许进分享包」的规则。
        self.assertFalse(any("references/nolicense.png" in n for n in s_names),
                         "未许可登记的图不得进分享包")
        self.assertTrue(any("references/nolicense.png" in n for n in p_names),
                        "未许可图仍应进私有档（本机私用）")

    def test_package_root_name_is_formatted(self):
        """包内根目录必须是实际包名，不得残留 {version} 模板占位符。"""
        for profile in ("private", "shareable"):
            zip_path = build_release(self.src, self.out, profile)
            with zipfile.ZipFile(zip_path) as zf:
                roots = {n.split("/")[0] for n in zf.namelist()}
            self.assertEqual(len(roots), 1, roots)
            root = roots.pop()
            self.assertNotIn("{version}", root)
            self.assertEqual(root, zip_path.stem)

    def test_private_package_manifest_declares_profile(self):
        private = build_release(self.src, self.out, "private")
        with zipfile.ZipFile(private) as zf:
            manifest_name = [n for n in zf.namelist() if n.endswith("发布清单.json")][0]
            data = json.loads(zf.read(manifest_name).decode("utf-8"))
        self.assertEqual(data["profile"], "private")
        self.assertTrue(data["style_pack_runtime_complete"])
        self.assertTrue(data["style_pack_assets_complete"])
        self.assertEqual(data["declarations"], [])

    def test_shareable_package_declares_incomplete_when_references_missing(self):
        """来源图被排除后缺运行必需参考时必须声明，不得声称与私人包等价。"""
        shareable = build_release(self.src, self.out, "shareable")
        with zipfile.ZipFile(shareable) as zf:
            manifest_name = [n for n in zf.namelist() if n.endswith("发布清单.json")][0]
            data = json.loads(zf.read(manifest_name).decode("utf-8"))
        self.assertEqual(data["profile"], "shareable")
        if not (data["style_pack_runtime_complete"] and data["style_pack_assets_complete"]):
            self.assertIn("VISUAL_STYLE_INCOMPLETE_FOR_SHARE", data["declarations"])

    def test_capability_statement_differs_between_profiles(self):
        """两包的能力声明必须不同（防止把分享包当私人包分发）。"""
        private = build_release(self.src, self.out, "private")
        shareable = build_release(self.src, self.out, "shareable")
        statements = {}
        for tag, zip_path in (("private", private), ("shareable", shareable)):
            with zipfile.ZipFile(zip_path) as zf:
                name = [n for n in zf.namelist() if n.endswith("发布清单.json")][0]
                statements[tag] = json.loads(zf.read(name).decode("utf-8"))["capability_statement"]
        self.assertNotEqual(statements["private"], statements["shareable"])
        self.assertIn("完整", statements["private"])
        self.assertIn("不含来源图", statements["shareable"])

    def test_shareable_lists_exclusions_with_reasons(self):
        shareable = build_release(self.src, self.out, "shareable")
        with zipfile.ZipFile(shareable) as zf:
            manifest_name = [n for n in zf.namelist() if n.endswith("发布清单.json")][0]
            data = json.loads(zf.read(manifest_name).decode("utf-8"))
        self.assertTrue(data["excluded"])
        for item in data["excluded"]:
            self.assertTrue(item["path"] and item["reason"])

    def test_both_profiles_never_contain_env_file(self):
        """.env 属排除物：两个 profile 都不得把它放进包里。"""
        import os

        (self.src / ".env").write_text("GEMINI_API_KEY=AIza" + "x" * 35, encoding="utf-8")
        for profile in ("private", "shareable"):
            zip_path = build_release(self.src, self.out, profile)
            names = self._names(zip_path)
            self.assertFalse([n for n in names if os.path.basename(n) == ".env"],
                             f"{profile} 包混入 .env")
            self.assertTrue(any(n.endswith(".env.example") for n in names),
                            ".env.example 模板应保留")

    def test_both_profiles_reject_absolute_desktop_paths(self):
        leak = self.src / "docs" / "泄漏.md"
        desktop = "参考 C:" + chr(92) + "Users" + chr(92) + "someone" + chr(92) \
            + "Desktop" + chr(92) + "a.png"
        leak.write_text(desktop, encoding="utf-8")
        for profile in ("private", "shareable"):
            with self.assertRaises(SystemExit):
                build_release(self.src, self.out, profile)

    def test_both_profiles_reject_key_literals(self):
        leak = self.src / "docs" / "密钥.md"
        leak.write_text("key = sk-" + "a" * 40, encoding="utf-8")
        for profile in ("private", "shareable"):
            with self.assertRaises(SystemExit):
                build_release(self.src, self.out, profile)

    def test_placeholder_values_do_not_block_release(self):
        """夹具里的占位密钥（fake/example 等）不得阻断发布；真实形状的密钥仍被拦。"""
        placeholder = self.src / "docs" / "占位.md"
        placeholder.write_text("OPENAI_API_KEY=sk-" + "fake-key-0123456789abcdef", encoding="utf-8")
        zip_path = build_release(self.src, self.out, "private")
        self.assertTrue(zip_path.is_file())

    def test_realistic_key_still_blocked(self):
        leak = self.src / "docs" / "真钥.md"
        leak.write_text("OPENAI_API_KEY=sk-" + "9f2b" * 10, encoding="utf-8")
        with self.assertRaises(SystemExit):
            build_release(self.src, self.out, "private")

    def test_unregistered_pack_image_rejected(self):
        orphan = self.src / "references/styles/风格X/1.0.0/private/references/orphan.png"
        orphan.write_bytes(bytes.fromhex("89504e470d0a1a0a") + b"unregistered")
        with self.assertRaises(SystemExit):
            build_release(self.src, self.out, "private")

    def test_unknown_profile_rejected(self):
        with self.assertRaises(SystemExit):
            build_release(self.src, self.out, "beta")

    def test_cli_profile_flag(self):
        from scripts.build_release import main

        rc = main(["--source", str(self.src), "--out", str(self.out), "--profile", "shareable"])
        self.assertEqual(rc, 0)
        self.assertTrue(list(self.out.glob("*shareable*.zip")))


if __name__ == "__main__":
    unittest.main()
