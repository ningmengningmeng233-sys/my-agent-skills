"""AT-03 回归：新库写库失败不再静默丢失转录稿。

原缺陷：新库只有 transcripts 表，record_transcript 访问 transcript_versions
必抛 sqlite3.OperationalError；bash 不检查退出码就删稿，并输出成功标记。
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import batch_transcribe as batch
from scripts import doctor
from scripts import record_transcript as rt
from scripts.transcript_db import TranscriptDB
from tests.support import SkillTestCase

SKILL_DIR = Path(__file__).resolve().parent.parent
TRANSCRIPT_SH = SKILL_DIR / "scripts" / "bilibili_transcript.sh"


def _make_inputs(root: Path) -> tuple[Path, Path]:
    meta = root / "meta.json"
    meta.write_text(json.dumps({
        "bvid": "BV1aaaaaaaaa", "page": 1, "cid": "", "part_title": "",
        "url": "https://www.bilibili.com/video/BV1aaaaaaaaa/",
        "title": "示例标题", "author": "示例作者", "duration": "30秒",
        "upload_date": "2026-01-02",
        "transcript_source": "faster-whisper small 模型（GPU加速）",
    }, ensure_ascii=False), encoding="utf-8")
    text = root / "text.txt"
    text.write_text("这是第一段\n这是第二段\n这是第三段\n", encoding="utf-8")
    return meta, text


def _tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


class FreshSchemaTests(SkillTestCase):
    """新库幂等建完整 schema；写库成功才算成功。"""

    def setUp(self):
        super().setUp()
        self.meta, self.text = _make_inputs(self.root)

    def test_at_03_contract(self):
        """全新库（文件尚不存在）写库必须成功并建全表。"""
        fresh = self.root / "fresh.db"
        args = ["--json", "--db", str(fresh), "--bvid", "BV1aaaaaaaaa",
                "--page", "1", "--meta", str(self.meta),
                "--text-file", str(self.text)]
        try:
            code, out = self.capture(rt.main, args)
        except sqlite3.OperationalError as exc:
            self.fail(f"新库写库失败（AT-03 未修复）: {exc}")
        self.assertEqual(code, 0)
        report = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(report["version_no"], 1)
        tables = _tables(fresh)
        for name in ("transcripts", "transcript_versions",
                     "transcript_segments", "schema_migrations"):
            self.assertIn(name, tables, f"新库缺表：{name}")
        conn = sqlite3.connect(str(fresh))
        try:
            row = conn.execute(
                "SELECT version_no, kind, is_current FROM transcript_versions"
                " WHERE bvid='BV1aaaaaaaaa'").fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(row), (1, "raw", 1))

    def test_legacy_db_is_healed_without_touching_rows(self):
        """旧版库（仅 transcripts 表 + 旧行）打开即补表，旧行原样保留。"""
        legacy = self.root / "legacy.db"
        conn = sqlite3.connect(str(legacy))
        conn.execute(
            "CREATE TABLE transcripts (bvid TEXT, page INTEGER, title TEXT,"
            " transcript_text TEXT, UNIQUE(bvid, page))")
        conn.execute(
            "INSERT INTO transcripts (bvid, page, title, transcript_text)"
            " VALUES ('BV1old', 1, '旧标题', '旧正文')")
        conn.commit()
        conn.close()
        with TranscriptDB(db_path=legacy):
            pass
        tables = _tables(legacy)
        for name in ("transcript_versions", "transcript_segments",
                     "schema_migrations"):
            self.assertIn(name, tables, f"旧库未补表：{name}")
        conn = sqlite3.connect(str(legacy))
        try:
            rows = conn.execute(
                "SELECT bvid, title, transcript_text FROM transcripts").fetchall()
            versions = conn.execute(
                "SELECT COUNT(*) FROM transcript_versions").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(rows, [("BV1old", "旧标题", "旧正文")])
        self.assertEqual(versions, 0, "补表不应顺手伪造版本行")


class DoctorSchemaTests(unittest.TestCase):
    def test_doctor_flags_db_missing_versions(self):
        """库存在但缺 transcript_versions 时 doctor 必须报 error。"""
        tmp = Path(tempfile.mkdtemp(prefix="at03_doctor_"))
        legacy = tmp / "legacy.db"
        conn = sqlite3.connect(str(legacy))
        conn.execute("CREATE TABLE transcripts (bvid TEXT, page INTEGER)")
        conn.commit()
        conn.close()
        with mock.patch.object(doctor, "DB_PATH", legacy):
            item = doctor.check_database()
        self.assertEqual(item["severity"], "error")
        self.assertFalse(item["ok"], "缺版本表的库不能再报绿")


class BatchMarkerTests(unittest.TestCase):
    """批量层只认写库成功后的机器可读标记。"""

    class _Result:
        def __init__(self, stdout, stderr=""):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = 0

    def _run(self, stdout: str):
        fake_db = mock.MagicMock()
        fake_db.__enter__.return_value = fake_db
        fake_db.get_by_bvid.return_value = {
            "transcript_source": "Whisper", "transcript_file": ""}
        with mock.patch.object(batch.subprocess, "run",
                               return_value=self._Result(stdout)), \
                mock.patch.object(batch, "TranscriptDB",
                                  return_value=fake_db), \
                mock.patch.object(batch, "OUTPUT_DIR",
                                  Path(tempfile.mkdtemp())), \
                contextlib.redirect_stdout(io.StringIO()):
            return batch.transcribe_video("BV1", 1)

    def test_new_marker_means_success(self):
        tmp = Path(tempfile.mkdtemp(prefix="at03_batch_"))
        delivered = tmp / "video_P01.txt"
        delivered.write_text("正文", encoding="utf-8")
        stdout = ("✅ Whisper 转写完成（待写库）\n"
                  f"📄 文件已保存: {delivered}\n"
                  f"{delivered}\n"
                  f"{batch.SUCCESS_MARKER} bvid=BV1 page=1 file={delivered}\n")
        ok, saved, _, _, _ = self._run(stdout)
        self.assertTrue(ok, "写库成功标记必须判为成功")
        self.assertEqual(saved, str(delivered))

    def test_stage_text_without_marker_is_not_success(self):
        """只有阶段性成功的旧式输出、没有完成标记时，不得判为成功。"""
        tmp = Path(tempfile.mkdtemp(prefix="at03_batch_"))
        delivered = tmp / "video_P01.txt"
        delivered.write_text("正文", encoding="utf-8")
        stdout = ("✅ 转录完成\n"
                  f"📄 文件已保存: {delivered}\n"
                  f"{delivered}\n")
        self.assertNotIn(batch.SUCCESS_MARKER, stdout)
        ok, _, _, _, _ = self._run(stdout)
        self.assertFalse(ok, "无完成标记不得判为成功")


class ShellContractTests(unittest.TestCase):
    """流水线脚本的静态契约：bash 整条链路禁跑真转录，此处只锁控制流。"""

    @classmethod
    def setUpClass(cls):
        cls.text = TRANSCRIPT_SH.read_text(encoding="utf-8")

    def test_record_failure_path_checks_exit_code(self):
        call = self.text.index("$SCRIPT_DIR/record_transcript.py")
        cleanup = self.text.index(
            'rm -f "$META_JSON" "$TEXT_TMP" "$TXT_FILE" "$SEGMENTS_FILE"',
            call)
        block = self.text[call:cleanup]
        self.assertIn("-ne 0", block, "写库调用后必须检查退出码")
        self.assertIn("exit 1", block, "写库失败必须以非零退出")

    def test_old_unconditional_success_echoes_are_gone(self):
        self.assertNotIn('echo "✅ 转录完成"\n', self.text)
        self.assertNotIn('echo "✅ 转录完成！"', self.text)

    def test_final_marker_is_emitted_after_db_write(self):
        self.assertIn(batch.SUCCESS_MARKER, self.text,
                      "脚本必须输出批量层识别的完成标记")
        call = self.text.index("$SCRIPT_DIR/record_transcript.py")
        marker = self.text.index(batch.SUCCESS_MARKER)
        self.assertGreater(marker, call, "完成标记必须在写库调用之后输出")


if __name__ == "__main__":
    unittest.main()
