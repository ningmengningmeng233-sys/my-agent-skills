"""模型资产清单测试：只校验与报告，绝不静默联网下载。"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts import model_assets
from scripts.model_assets import AssetState, ModelAssetManager

BODIES = {
    "state/models/ocr/det.onnx": b"det!",
    "state/models/sherpa/uvr/vocals.onnx": b"voc!",
}


def build_manifest() -> dict:
    """清单里的 sha256/size 由实际字节算出，避免测试数据与断言互相矛盾。"""
    models = []
    for name, capability, relative, required, url in (
        ("ocr_det", "ocr", "state/models/ocr/det.onnx", True, ""),
        ("uvr_vocals", "vocals", "state/models/sherpa/uvr/vocals.onnx", False,
         "https://example.test/vocals.onnx"),
    ):
        body = BODIES[relative]
        models.append({"name": name, "capability": capability,
                       "relative_path": relative, "size_bytes": len(body),
                       "sha256": hashlib.sha256(body).hexdigest(),
                       "source": "test", "required": required, "url": url})
    return {"schema_version": 1, "models": models}


MANIFEST = build_manifest()


class AssetManagerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.manifest = json.loads(json.dumps(MANIFEST))
        self.files = dict(BODIES)
        self.manager = ModelAssetManager(root=self.root,
                                         manifest=self.manifest)

    def _materialize(self, relative: str, body: bytes | None = None) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if body is not None else self.files[relative])
        return path


class VerifyTests(AssetManagerTestCase):
    def test_missing_model_is_reported_with_install_command(self):
        state = self.manager.verify("ocr_det")
        self.assertEqual(state.status, "missing")
        self.assertFalse(state.ok)
        self.assertTrue(state.required)
        self.assertTrue(state.fix_command)
        self.assertIn("model_assets", state.fix_command)

    def test_present_and_matching_model_is_ok(self):
        self._materialize("state/models/ocr/det.onnx")
        state = self.manager.verify("ocr_det")
        self.assertTrue(state.ok)
        self.assertEqual(state.status, "ok")
        self.assertEqual(state.size_bytes, 4)

    def test_hash_mismatch_is_reported(self):
        self._materialize("state/models/ocr/det.onnx", b"evil")
        state = self.manager.verify("ocr_det")
        self.assertEqual(state.status, "corrupt")
        self.assertFalse(state.ok)
        self.assertIn("哈希", state.detail)

    def test_wrong_size_is_reported_as_corrupt(self):
        self._materialize("state/models/ocr/det.onnx", b"det!!")
        self.assertEqual(self.manager.verify("ocr_det").status, "corrupt")

    def test_unknown_name_raises(self):
        with self.assertRaises(KeyError):
            self.manager.verify("nope")

    def test_optional_model_missing_is_not_fatal_but_reported(self):
        state = self.manager.verify("uvr_vocals")
        self.assertFalse(state.ok)
        self.assertFalse(state.required)
        self.assertEqual(state.severity, "warning")

    def test_required_model_missing_is_error_severity(self):
        self.assertEqual(self.manager.verify("ocr_det").severity, "error")


class VerifyAllTests(AssetManagerTestCase):
    def test_report_groups_by_capability(self):
        self._materialize("state/models/ocr/det.onnx")
        report = self.manager.verify_all()
        self.assertFalse(report["ok"])  # 可选模型缺失仍让核心降级
        self.assertEqual(report["missing_optional"], 1)
        self.assertEqual(report["broken_required"], 0)
        self.assertEqual(report["states"]["ocr_det"].status, "ok")
        self.assertEqual(report["states"]["uvr_vocals"].status, "missing")

    def test_all_ready_when_files_match(self):
        for relative in self.files:
            self._materialize(relative)
        report = self.manager.verify_all()
        self.assertTrue(report["ok"])
        self.assertEqual(report["missing_optional"], 0)

    def test_capability_usable_false_when_corrupt(self):
        self._materialize("state/models/ocr/det.onnx", b"evil")
        self.assertFalse(self.manager.capability_usable("ocr"))

    def test_capability_usable_true_for_optional_when_present(self):
        self._materialize("state/models/sherpa/uvr/vocals.onnx")
        self.assertTrue(self.manager.capability_usable("vocals"))


class ManifestLoadingTests(unittest.TestCase):
    def test_real_manifest_loads_and_paths_are_relative(self):
        manager = ModelAssetManager(root=Path("."))
        names = {entry.name for entry in manager.entries()}
        self.assertIn("uvr_vocals", names)
        self.assertIn("diarization_embedding", names)
        self.assertIn("ocr_rec", names)
        for entry in manager.entries():
            self.assertFalse(Path(entry.relative_path).is_absolute())
            self.assertTrue(entry.sha256 and len(entry.sha256) == 64)

    def test_real_manifest_hashes_match_local_files_when_present(self):
        manager = ModelAssetManager(root=Path("."))
        for entry in manager.entries():
            state = manager.verify(entry.name)
            self.assertIn(state.status, ("ok", "missing"),
                          f"{entry.name} 状态异常：{state.detail}")

    def test_manifest_records_official_sources(self):
        manager = ModelAssetManager(root=Path("."))
        for entry in manager.entries():
            self.assertTrue(entry.source, f"{entry.name} 缺少来源说明")
            if entry.capability in ("vocals", "diarization"):
                self.assertTrue(entry.url.startswith("https://github.com/k2-fsa/"))


class NoSilentDownloadTests(AssetManagerTestCase):
    def test_verify_never_touches_network(self):
        with unittest.mock.patch("urllib.request.urlopen",
                                 side_effect=AssertionError("禁止联网")) as fake, \
             unittest.mock.patch("requests.get",
                                 side_effect=AssertionError("禁止联网")):
            self.manager.verify_all()
            fake.assert_not_called()

    def test_sync_only_copies_from_local_wheel(self):
        # sync 只从已安装的 rapidocr 包复制模型，不下载
        source = self.root / "site-packages" / "ocr" / "det.onnx"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"det!")
        with unittest.mock.patch("urllib.request.urlopen",
                                 side_effect=AssertionError("禁止联网")):
            copied = self.manager.sync_from_local("ocr_det", source)
        self.assertTrue(copied.is_file())
        self.assertEqual(self.manager.verify("ocr_det").status, "ok")

    def test_download_plan_only_describes_commands(self):
        plan = self.manager.download_plan()
        self.assertTrue(plan)
        self.assertTrue(all(item["url"].startswith("http") for item in plan))
        self.assertTrue(all("sha256" in item and "relative_path" in item
                            for item in plan))


class AssetStateTests(unittest.TestCase):
    def test_state_is_frozen_and_json_ready(self):
        state = AssetState(name="x", status="ok", path="p", detail="d",
                           required=True, severity="error",
                           fix_command="", size_bytes=1)
        with self.assertRaises(Exception):
            state.status = "missing"
        self.assertEqual(state.as_dict()["name"], "x")


from unittest import mock  # noqa: E402  （供上面的 patch 使用）


class CliTests(unittest.TestCase):
    def test_cli_json_reports_states(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock as cli_mock

        with tempfile.TemporaryDirectory() as tmp:
            manager = ModelAssetManager(root=Path(tmp),
                                        manifest=json.loads(json.dumps(MANIFEST)))
            required = manager.path_of("ocr_det")
            required.parent.mkdir(parents=True, exist_ok=True)
            required.write_bytes(b"det!")
            with cli_mock.patch.object(model_assets, "ModelAssetManager",
                                       return_value=manager), \
                 redirect_stdout(io.StringIO()) as out:
                code = model_assets.main(["--json"])
            payload = json.loads(out.getvalue().strip())
        self.assertEqual(code, 0)  # 缺可选模型不改变退出码
        self.assertIn("states", payload)
        self.assertIn("download_plan", payload)
        self.assertEqual(payload["states"]["ocr_det"]["status"], "ok")

    def test_cli_exit_code_is_nonzero_when_required_model_broken(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock as cli_mock

        with tempfile.TemporaryDirectory() as tmp:
            manager = ModelAssetManager(root=Path(tmp),
                                        manifest=json.loads(json.dumps(MANIFEST)))
            with cli_mock.patch.object(model_assets, "ModelAssetManager",
                                       return_value=manager), \
                 redirect_stdout(io.StringIO()):
                code = model_assets.main(["--json"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
