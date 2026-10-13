"""事务式发布：任一点失败都必须保住旧文件与旧 current 版本。"""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import artifacts
from scripts.artifacts import (publish_artifacts, render_evidence, render_main)
from scripts.models import ArtifactKind, QualityStatus
from scripts.transcript_db import PublishError, TranscriptDB
from tests.test_artifacts import AWEME, SEGMENTS, TITLE


class PublishTransactionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = TranscriptDB(self.root / ".db" / "t.db")
        self.addCleanup(self.db.close)
        self.output = self.root / "output"
        self.old_version = self._seed(version_no_hint="旧版本")
        self._publish(self.old_version)
        self.old_main = Path(self._current_paths().main_file)
        self.old_evidence = Path(self._current_paths().evidence_file)
        self.old_main_body = self.old_main.read_text(encoding="utf-8")
        self.old_evidence_body = self.old_evidence.read_text(encoding="utf-8")
        self.new_version = self._seed(version_no_hint="新版本")

    def _seed(self, *, version_no_hint: str) -> int:
        version_id = self.db.create_staging_version(
            AWEME, metadata={"canonical_url": f"https://www.douyin.com/video/{AWEME}",
                             "title": TITLE, "author": "奶龙",
                             "publish_date": "2026-06-19", "duration_seconds": 70.0})
        segments = [dict(item, text=f"{version_no_hint}-{item['text']}")
                    for item in SEGMENTS]
        self.db.add_asr_attempt(
            version_id, attempt_no=1, audio_variant="original", strategy={},
            model="medium", device="cuda", compute_type="float16", language="zh",
            transcript_text="\n".join(item["text"] for item in segments),
            segments_json=segments, metrics={}, hard_failures=[], score=88.0,
            passed=True)
        attempt = self.db.list_attempts(version_id)[0]
        self.db.select_attempt(version_id, attempt["attempt_id"])
        self.db.set_quality(version_id, status=QualityStatus.PASSED, score=88.0,
                            report={})
        self.db.save_correction(version_id, corrected_text=version_no_hint,
                                changes=[],
                                status="corrected")
        self.db.save_summary(version_id, summary=f"**核心观点：**\n{version_no_hint}",
                             status="summarized", mode="normal", meta={})
        return version_id

    def _publish(self, version_id: int):
        main = render_main(artifacts.build_version_bundle(self.db, version_id),
                           self.output)
        evidence = render_evidence(
            artifacts.build_version_bundle(self.db, version_id), self.output)
        return publish_artifacts(self.db, version_id, [main, evidence])

    def _current_paths(self):
        version = self.db.get_current_version(AWEME)
        artifacts_by_kind = version["artifacts"]
        return type("Paths", (), {
            "main_file": artifacts_by_kind["main_txt"]["path"],
            "evidence_file": artifacts_by_kind["evidence_txt"]["path"]})()

    def _assert_old_state_intact(self):
        self.assertEqual(self.db.get_current_version(AWEME)["version_id"],
                         self.old_version)
        self.assertEqual(self.old_main.read_text(encoding="utf-8"),
                         self.old_main_body)
        self.assertEqual(self.old_evidence.read_text(encoding="utf-8"),
                         self.old_evidence_body)
        self.assertEqual([p.name for p in self.output.rglob("*.tmp")], [])
        self.assertEqual(self.db.get_version(self.new_version)["is_current"], 0)

    def _new_prepared(self):
        bundle = artifacts.build_version_bundle(self.db, self.new_version)
        return [render_main(bundle, self.output),
                render_evidence(bundle, self.output)]


class FailureInjectionTests(PublishTransactionTestCase):
    def test_main_file_write_failure_keeps_old_state(self):
        bundle = artifacts.build_version_bundle(self.db, self.new_version)
        real = artifacts.write_prepared_text

        def flaky(path, text, **kwargs):
            if "_原始记录" not in Path(path).name:
                raise OSError("磁盘满")
            return real(path, text, **kwargs)

        with mock.patch.object(artifacts, "write_prepared_text",
                               side_effect=flaky):
            with self.assertRaises(OSError):
                render_main(bundle, self.output)
        self._assert_old_state_intact()

    def test_evidence_file_write_failure_keeps_old_state(self):
        bundle = artifacts.build_version_bundle(self.db, self.new_version)
        with mock.patch.object(artifacts, "write_prepared_text",
                               side_effect=OSError("磁盘满")):
            with self.assertRaises(OSError):
                render_evidence(bundle, self.output)
        self._assert_old_state_intact()

    def test_hash_verification_failure_keeps_old_state(self):
        prepared = self._new_prepared()
        Path(prepared[0].path).write_text("发布前被篡改", encoding="utf-8")
        with self.assertRaises(PublishError):
            publish_artifacts(self.db, self.new_version, prepared)
        self._assert_old_state_intact()

    def test_database_failure_keeps_old_state_and_restores_files(self):
        prepared = self._new_prepared()
        with mock.patch.object(self.db, "publish_version",
                               side_effect=PublishError("注入的数据库失败")):
            with self.assertRaises(PublishError):
                publish_artifacts(self.db, self.new_version, prepared)
        self._assert_old_state_intact()

    def test_replace_failure_on_second_file_restores_the_first(self):
        prepared = self._new_prepared()
        calls = {"n": 0}
        real_replace = artifacts.os.replace

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("重命名失败")
            return real_replace(src, dst)

        with mock.patch.object(artifacts.os, "replace", side_effect=flaky_replace):
            with self.assertRaises(PublishError):
                publish_artifacts(self.db, self.new_version, prepared)
        self._assert_old_state_intact()

    def test_missing_prepared_file_is_refused(self):
        prepared = self._new_prepared()
        Path(prepared[1].path).unlink()
        with self.assertRaises(PublishError):
            publish_artifacts(self.db, self.new_version, prepared)
        self._assert_old_state_intact()

    def test_wrong_kinds_are_refused(self):
        prepared = self._new_prepared()
        with self.assertRaises(PublishError):
            publish_artifacts(self.db, self.new_version, [prepared[0]])
        self._assert_old_state_intact()


class SuccessfulPublishTests(PublishTransactionTestCase):
    def test_successful_publish_switches_current_and_files(self):
        published = self._publish(self.new_version)
        self.assertEqual(self.db.get_current_version(AWEME)["version_id"],
                         self.new_version)
        self.assertIn("新版本", Path(published.main_file).read_text(encoding="utf-8"))
        self.assertNotEqual(Path(published.main_file).read_text(encoding="utf-8"),
                            self.old_main_body)
        self.assertEqual([p.name for p in self.output.rglob("*.tmp")], [])

    def test_old_version_rows_are_kept(self):
        self._publish(self.new_version)
        versions = self.db.list_versions(AWEME)
        self.assertEqual([item["version_id"] for item in versions],
                         [self.old_version, self.new_version])
        self.assertEqual(self.db.get_version(self.old_version)["is_current"], 0)

    def test_reuse_action_verified_after_publish(self):
        from scripts.models import ReuseAction

        self._publish(self.new_version)
        self.assertEqual(self.db.find_reuse_action(AWEME), ReuseAction.REUSE)

    def test_publishing_twice_is_idempotent(self):
        published = self._publish(self.new_version)
        main = Path(published.main_file)
        raw = main.read_bytes()
        again = self._publish(self.new_version)
        self.assertEqual(main.read_bytes(), raw)
        self.assertEqual(self.db.get_current_version(AWEME)["version_id"],
                         self.new_version)
        self.assertEqual(hashlib.sha256(main.read_bytes()).hexdigest(),
                         again.main_sha256)

    def test_artifact_kinds_are_both_registered(self):
        self._publish(self.new_version)
        kinds = {item["kind"] for item in
                 self.db.list_artifacts(self.new_version)}
        self.assertEqual(kinds, {str(ArtifactKind.MAIN_TXT),
                                str(ArtifactKind.EVIDENCE_TXT)})


if __name__ == "__main__":
    unittest.main()
